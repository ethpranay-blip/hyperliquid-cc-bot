"""
NiceGUI dashboard + event loop for the Corgi Calls Copy Trading Bot.

Runs at http://localhost:8080 with:
- Stats header (Total PnL / Win Rate / Open Count / DRY RUN banner)
- Auto-mode toggle (per-session, default OFF)
- Active trade cards (LIVE + pending), with live mid & unrealized PnL
- Historic trades table
- Right sidebar: real-time activity feed

Background tasks (never block the UI):
- portal poll loop (~3 s) → routes events to handlers
- HL price feed (WebSocket) → shared price dict
- UI refresh timers
"""

from __future__ import annotations

# Load .env BEFORE importing any module that reads environment at import time
# (notifier reads NOTIFY_WEBHOOK_URL on import; other modules read env in
# constructors, which are called later, but loading first keeps everything
# consistent).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import asyncio
import logging
import logging.handlers
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from nicegui import app, ui

from app import db
from app import notifier
from app.hyperliquid_client import (
    HyperliquidClient, HyperliquidError, HyperliquidValidationError,
    hl_symbol_for,
)
from app.portal import PortalClient, PortalAuthError


# ============================================================
# SECTION: Logging
# ============================================================

def _setup_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    # Clear handlers in case of re-import
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(level)
    root.addHandler(console)

    try:
        fh = logging.handlers.RotatingFileHandler(
            "app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        fh.setFormatter(fmt)
        fh.setLevel(level)
        root.addHandler(fh)
    except Exception as exc:  # pragma: no cover
        logging.getLogger(__name__).warning("file logger setup failed: %s", exc)


_setup_logging()
log = logging.getLogger("main")


# ============================================================
# SECTION: Global app state
# ============================================================

class AppState:
    def __init__(self) -> None:
        self.hl: Optional[HyperliquidClient] = None
        self.portal: Optional[PortalClient] = None
        self.portal_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.reconcile_task: Optional[asyncio.Task] = None
        self.hl_change_task: Optional[asyncio.Task] = None
        # Trade IDs the user has explicitly approved for entry even though
        # they're STALE (older than startup-time cutoff). Loaded from
        # FORCE_ENTER_TIDS env at startup; can be appended to at runtime.
        self.force_enter_tids: set[int] = set()
        # Set by HL userEvents WS callback when ANY user-side change happens
        # (fill, manual close, manual SL move, liquidation). The
        # hl_change_reconciler debounces and runs reconcile_on_startup
        # within ~2s of the trigger.
        self.hl_change_event: Optional[asyncio.Event] = None
        # per-process (resets on restart). Controlled by AUTO_MODE env var.
        self.auto_mode: bool = os.environ.get("AUTO_MODE", "").lower() in ("1", "true", "yes")
        self.pending_trades: dict[int, dict] = {}  # trade_id -> new_trade event
        self.activity_feed: deque[dict] = deque(maxlen=200)
        self.dry_run: bool = True
        self.refresh_callbacks: list = []
        # Unix-ms timestamp captured at on_startup; any trade whose portal
        # timestamp is older than this is treated as "stale" backlog — it
        # appears on the dashboard (so SL/TP/close events still route) but
        # will not auto-enter and the Enter button is disabled.
        # A small slack window (STALE_SLACK_MS) allows trades that arrive
        # in the same activity-feed batch as startup to still count as fresh.
        self.startup_time_ms: int = 0

    def register_refresh(self, fn) -> None:
        if fn not in self.refresh_callbacks:
            self.refresh_callbacks.append(fn)

    def fire_refresh(self) -> None:
        for fn in list(self.refresh_callbacks):
            try:
                fn()
            except Exception:
                log.exception("refresh callback failed")


state = AppState()

# How far back (in ms) a trade's timestamp can be relative to bot startup
# and still count as "fresh". 5 minutes gives room for poll-loop latency and
# clock skew between the portal and this host.
STALE_SLACK_MS = 5 * 60 * 1000


# ============================================================
# SECTION: Activity feed helpers
# ============================================================

def _push_activity(kind: str, text: str, trade_id: Optional[int] = None) -> None:
    state.activity_feed.appendleft({
        "kind": kind,
        "text": text,
        "trade_id": trade_id,
        "at": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    })
    state.fire_refresh()


def _fmt_price(p) -> str:
    if p is None:
        return "—"
    try:
        p = float(p)
    except (ValueError, TypeError):
        return str(p)
    if abs(p) >= 1000:
        return f"{p:,.2f}"
    if abs(p) >= 1:
        return f"{p:.4f}"
    return f"{p:.8f}".rstrip("0").rstrip(".") or "0"


def _fmt_pnl(p) -> str:
    if p is None:
        return "—"
    try:
        p = float(p)
    except (ValueError, TypeError):
        return str(p)
    sign = "+" if p >= 0 else "-"
    return f"{sign}${abs(p):,.2f}"


# ============================================================
# SECTION: Event handlers (portal → HL + DB + notifier)
# ============================================================

def _enrich_event_from_detail(event: dict, detail: dict) -> dict:
    """Merge portal trade-detail fields into an event dict.

    The activity-feed event only carries coin/side/caller/entryRaw/tradeId.
    The detail (from POST /me/trades or GET /me/trades) adds stop, tp,
    leverage, entryUsed, originalStop. Overwrite only when the enriched
    value is non-null so we never clobber good data with missing data.
    """
    if not isinstance(detail, dict):
        return event

    def _num_or(v, fallback):
        if v is None or v == "":
            return fallback
        try:
            return float(v)
        except (ValueError, TypeError):
            return fallback

    entry = _num_or(detail.get("entryUsed"), _num_or(detail.get("entryRaw"), event.get("entry_price")))
    stop = _num_or(detail.get("stop"), event.get("stop_loss"))
    lev = _num_or(detail.get("leverage"), event.get("leverage"))

    # Normalize take-profits: the portal returns either a scalar, a list of
    # numbers, or a list of dicts — accept them all.
    tps_raw = detail.get("tp")
    tps: list = []
    if isinstance(tps_raw, list):
        tps = tps_raw
    elif tps_raw is not None:
        tps = [tps_raw]
    elif event.get("take_profits"):
        tps = event["take_profits"]

    enriched = dict(event)
    if entry is not None:
        enriched["entry_price"] = entry
    if stop is not None:
        enriched["stop_loss"] = stop
    if lev is not None:
        enriched["leverage"] = lev
    if tps:
        enriched["take_profits"] = tps
    enriched["portal_detail"] = detail
    return enriched


def _event_is_stale(event: dict) -> bool:
    """Return True if this trade's portal timestamp predates bot startup.

    Stale trades still appear on the dashboard (so subsequent SL/TP/close
    events still route) but can never be entered — neither by auto-mode nor
    by a manual click. Uses STALE_SLACK_MS slack to avoid false positives
    on the very first poll after startup.

    If the event has no timestamp at all, we treat it as stale (conservative).

    OVERRIDE: trade IDs in FORCE_ENTER_TIDS env (or runtime-added to
    state.force_enter_tids) bypass the stale check. Use this to take a
    legitimate but old call that was missed during downtime.
    """
    tid = event.get("trade_id")
    if tid is not None and int(tid) in state.force_enter_tids:
        return False
    if not state.startup_time_ms:
        return False  # startup hasn't initialized cutoff yet
    at = event.get("at")
    try:
        at_ms = int(at) if at is not None else 0
    except (ValueError, TypeError):
        at_ms = 0
    if at_ms == 0:
        return True  # missing timestamp → conservative
    cutoff = state.startup_time_ms - STALE_SLACK_MS
    return at_ms < cutoff


async def handle_new_trade(event: dict) -> None:
    trade_id = event.get("trade_id")
    coin = event.get("coin")
    side = event.get("side") or "long"
    caller = event.get("caller")
    log.info("handle_new_trade: #%s %s %s caller=%s", trade_id, coin, side, caller)
    if trade_id is None or not coin:
        log.warning("new_trade missing trade_id/coin: %r", event)
        return

    db.insert_portal_event(
        event_type="enter", trade_id=trade_id, coin=coin, side=side,
        caller=caller, details=event.get("raw"),
    )
    _push_activity("new_trade", f"NEW {coin} {side.upper()} from {caller} #{trade_id}", trade_id)

    # Dedup: don't re-add if already live, already closed, or pending.
    # The activity-feed backlog still contains trade_open_* events for trades
    # that we've already processed and closed. On a bot restart (in-memory
    # _seen_event_ids resets), those old opens would otherwise be replayed as
    # real orders. Checking hl_closed_trades prevents re-entering a closed
    # trade_id; checking hl_live_trades prevents double-open of a live one.
    if db.get_live_trade(int(trade_id)) is not None:
        log.info("new_trade #%s already live — skipping", trade_id)
        return
    if db.get_closed_trade(int(trade_id)) is not None:
        log.info("new_trade #%s already closed in DB — skipping replay", trade_id)
        return
    if int(trade_id) in state.pending_trades:
        return

    # Stale check — trades posted before bot started can't be auto/manually entered
    stale = _event_is_stale(event)

    # Enrich with full portal detail (stop, tp, leverage) via follow-and-fetch.
    # Fall back to raw event (env defaults) if the enrichment call fails.
    # Skip enrichment for stale trades — we can't enter them anyway, and
    # follow-POST'ing every stale backlog trade just spams the portal.
    if not stale and state.portal is not None:
        try:
            detail = await state.portal.get_trade_detail(int(trade_id))
            if detail:
                event = _enrich_event_from_detail(event, detail)
                log.info(
                    "enriched #%s: entry=%s stop=%s leverage=%s tps=%s",
                    trade_id,
                    event.get("entry_price"),
                    event.get("stop_loss"),
                    event.get("leverage"),
                    event.get("take_profits"),
                )
            else:
                # Typically a closed/invalid trade — portal logs the cause at DEBUG.
                # Demoted from WARNING to keep the log clean on backlog fetches.
                log.debug("no detail for #%s — will use env defaults", trade_id)
        except Exception:
            log.exception("enrichment failed for #%s — will use env defaults", trade_id)

    # Stash the stale flag on the event so UI + enter_trade can honor it
    event["stale"] = stale

    state.pending_trades[int(trade_id)] = event
    state.fire_refresh()

    if stale:
        log.info(
            "new_trade #%s %s is STALE (posted before bot start) — "
            "dashboard card only, no entry",
            trade_id, coin,
        )
        _push_activity(
            "stale", f"STALE {coin} {side.upper()} #{trade_id}", trade_id,
        )
        return

    # Auto-mode: open immediately if coin not already live
    if state.auto_mode:
        if db.is_coin_live(coin):
            log.info("auto-mode: %s already live → BLOCKED for #%s", coin, trade_id)
            _push_activity("blocked", f"BLOCKED {coin} (already live) #{trade_id}", trade_id)
            return
        log.info("auto-mode: entering #%s", trade_id)
        await enter_trade(trade_id)


async def _auto_trail_stop_after_tp(
    trade_id: int, coin: str, side: str, tp_num: Optional[int], opened: dict
) -> None:
    """
    Automatically trail stop after TP hit per Corgi Calls rules:
    - TP1 hit → Move stop to entry (BE)
    - TP2 hit → Keep stop at entry (BE)
    - TP3 hit → Move stop to TP1 price
    - TP4 hit → Move stop to TP2 price
    """
    if tp_num is None or state.hl is None:
        return

    entry = opened.get("entry_price")
    if entry is None:
        return

    # Determine new stop based on TP number
    new_stop = None

    if tp_num in (1, 2):
        # TP1 or TP2 hit → move to breakeven
        new_stop = float(entry)
        reason = f"TP{tp_num} hit → BE"

    elif tp_num == 3:
        # TP3 hit → move to TP1 price
        tp1_price = opened.get("tp1")
        if tp1_price is not None:
            new_stop = float(tp1_price)
            reason = "TP3 hit → TP1"

    elif tp_num == 4:
        # TP4 hit → move to TP2 price
        tp2_price = opened.get("tp2")
        if tp2_price is not None:
            new_stop = float(tp2_price)
            reason = "TP4 hit → TP2"

    if new_stop is None:
        return

    # Update stop on Hyperliquid
    try:
        log.info(
            "AUTO TRAILING STOP: #%s %s - %s (new_stop=%.4f)",
            trade_id, coin, reason, new_stop
        )

        await state.hl.update_stop(
            trade_id=trade_id, portal_coin=coin, side=side,
            new_portal_stop=new_stop, portal_entry=float(entry),
        )

        db.insert_sl_update(
            trade_id=trade_id,
            old_stop=opened.get("entry_sl"),
            new_stop=new_stop,
        )

        _push_activity(
            "stop_update",
            f"🔒 AUTO SL→{_fmt_price(new_stop)} {coin} #{trade_id} ({reason})",
            trade_id
        )

        log.info("AUTO TRAILING: SL updated #%s → %.4f (%s)", trade_id, new_stop, reason)

    except Exception:
        log.exception("AUTO TRAILING: update_stop failed #%s", trade_id)


async def handle_stop_update(event: dict) -> None:
    trade_id = event.get("trade_id")
    new_stop = event.get("new_stop")
    if trade_id is None or new_stop is None:
        return
    opened = db.get_opened_trade(int(trade_id))
    if opened is None:
        return  # not one of ours

    coin = opened["coin"]
    side = opened["side"]
    entry = opened["entry_price"]

    _push_activity("stop_update", f"SL→{_fmt_price(new_stop)} {coin} #{trade_id}", trade_id)
    db.insert_portal_event(
        event_type="enter", trade_id=trade_id, coin=coin, side=side,
        caller=opened.get("caller"), details={"stop_update": new_stop},
    )

    if state.hl is None:
        return
    try:
        await state.hl.update_stop(
            trade_id=int(trade_id), portal_coin=coin, side=side,
            new_portal_stop=float(new_stop), portal_entry=float(entry),
        )
        db.insert_sl_update(
            trade_id=int(trade_id), old_stop=opened.get("entry_sl"),
            new_stop=float(new_stop),
        )
        log.info("SL updated #%s → %s", trade_id, new_stop)
    except Exception:
        log.exception("update_stop failed #%s", trade_id)


async def handle_tp_hit(event: dict) -> None:
    trade_id = event.get("trade_id")
    size_pct = event.get("size_pct")
    tp_price = event.get("tp_price")
    tp_num = event.get("tp_num")
    if trade_id is None or size_pct is None:
        return
    if not db.get_live_trade(int(trade_id)):
        return  # not ours or already closed

    opened = db.get_opened_trade(int(trade_id))
    if opened is None:
        return
    coin = opened["coin"]
    side = opened["side"]

    db.insert_portal_event(
        event_type="tp_hit", trade_id=trade_id, coin=coin, side=side,
        caller=opened.get("caller"),
        details={"size_pct": size_pct, "tp_price": tp_price, "tp_num": tp_num},
    )
    _push_activity("tp_hit", f"TP{tp_num or ''} hit {size_pct}% {coin} #{trade_id}", trade_id)

    if state.hl is None:
        return

    try:
        if float(size_pct) >= 100:
            # Treated as full close
            result = await state.hl.close_trade(
                trade_id=int(trade_id), portal_coin=coin, side=side,
            )
            _finalize_close(int(trade_id), opened, result, close_type="automatic")
        else:
            result = await state.hl.partial_tp(
                trade_id=int(trade_id), portal_coin=coin, side=side,
                size_pct=float(size_pct),
            )
            db.insert_tp_update(
                trade_id=int(trade_id),
                tp_price=float(tp_price) if tp_price is not None else float(result.avg_exit_price or 0),
                tp_pct=float(size_pct), tp_num=tp_num,
                size=result.size, fee=result.fee,
            )
            notifier.notify_tp_hit(
                coin=coin, side=side, tp_price=tp_price,
                size_pct=float(size_pct), tp_num=tp_num,
                trade_id=int(trade_id), dry_run=state.dry_run,
            )

            # CRITICAL: Automatic trailing stop after TP hit
            # Corgi Calls rules: TP1/TP2 → BE, TP3 → TP1, TP4 → TP2
            await _auto_trail_stop_after_tp(
                trade_id=int(trade_id), coin=coin, side=side,
                tp_num=tp_num, opened=opened
            )

    except HyperliquidValidationError as exc:
        log.warning("TP execution rejected #%s: %s", trade_id, exc)
    except Exception:
        log.exception("tp_hit handler failed #%s", trade_id)

    state.fire_refresh()


async def handle_full_close(event: dict) -> None:
    trade_id = event.get("trade_id")
    stop_triggered = bool(event.get("stop_triggered"))
    if trade_id is None:
        return
    if not db.get_live_trade(int(trade_id)):
        return

    opened = db.get_opened_trade(int(trade_id))
    if opened is None:
        return
    coin = opened["coin"]
    side = opened["side"]

    evt_type = "sl_triggered" if stop_triggered else "auto_close"
    db.insert_portal_event(
        event_type=evt_type, trade_id=trade_id, coin=coin, side=side,
        caller=opened.get("caller"), details=event.get("raw"),
    )
    _push_activity(
        "sl_triggered" if stop_triggered else "close",
        f"{'SL TRIGGERED' if stop_triggered else 'CLOSED'} {coin} #{trade_id}",
        trade_id,
    )

    if state.hl is None:
        return

    try:
        result = await state.hl.close_trade(
            trade_id=int(trade_id), portal_coin=coin, side=side,
        )
        _finalize_close(
            int(trade_id), opened, result,
            close_type="stop_triggered" if stop_triggered else "automatic",
        )
    except HyperliquidValidationError as exc:
        log.warning("close rejected #%s (likely already closed): %s", trade_id, exc)
        db.remove_live_trade(int(trade_id))
    except Exception:
        log.exception("full_close handler failed #%s", trade_id)

    state.fire_refresh()


def _finalize_close(trade_id: int, opened: dict, result, close_type: str) -> None:
    coin = opened["coin"]
    side = opened["side"]
    entry = float(opened["entry_price"])
    exit_price = float(result.avg_exit_price or 0) or entry
    size = float(result.size)
    fee = result.fee
    pnl = result.pnl
    trade_value = exit_price * size

    db.insert_closed_trade(
        trade_id=trade_id, coin=coin, side=side,
        entry_price=entry, exit_price=exit_price,
        size=size, trade_value=trade_value,
        margin=opened.get("margin"), fee=fee, pnl=pnl,
        close_type=close_type,
    )
    db.remove_live_trade(trade_id)

    is_sl = close_type == "stop_triggered"
    if is_sl:
        notifier.notify_sl_triggered(
            coin=coin, side=side, entry=entry, stop=opened.get("entry_sl"),
            pnl=pnl, trade_id=trade_id, dry_run=state.dry_run,
        )
    else:
        notifier.notify_closed(
            coin=coin, side=side, entry=entry, exit=exit_price,
            pnl=pnl, fee=fee, close_type=close_type, trade_id=trade_id,
            dry_run=state.dry_run,
        )


# ============================================================
# SECTION: Manual actions (button handlers)
# ============================================================

def _safe_notify(msg: str, type: str = "info") -> None:
    """ui.notify() that won't crash when called outside a UI request context.

    In auto-mode, enter_trade() runs from the portal poll task and has no
    client bound — ui.notify would raise. This wrapper swallows that case
    and always logs the message so nothing is lost.
    """
    log_fn = {
        "negative": log.warning,
        "warning":  log.warning,
        "positive": log.info,
        "info":     log.info,
    }.get(type, log.info)
    log_fn("notify[%s]: %s", type, msg)
    try:
        ui.notify(msg, type=type)
    except Exception:
        # No UI client context — logging above is sufficient.
        pass


async def enter_trade(trade_id: int) -> None:
    event = state.pending_trades.get(int(trade_id))
    if event is None:
        _safe_notify(f"Trade #{trade_id} not in pending queue", "warning")
        return

    # STALE guard — a stale trade (portal timestamp < bot startup) cannot be
    # entered by any path. This is a hard block, not a soft warning.
    if event.get("stale"):
        _safe_notify(
            f"STALE: #{trade_id} posted before bot start — entry blocked",
            "warning",
        )
        return

    coin = event["coin"]
    side = event.get("side") or "long"
    entry = event.get("entry_price")
    stop = event.get("stop_loss")
    caller = event.get("caller")

    if db.is_coin_live(coin):
        _safe_notify(f"BLOCKED: {coin} already live", "warning")
        return
    if state.hl is None:
        _safe_notify("HL client not ready", "negative")
        return

    # ── PRE-FLIGHT MARGIN CHECK ──
    # Check withdrawable BEFORE submitting. If insufficient, DROP the signal
    # cleanly (don't queue, don't backfill later). When margin frees up,
    # only NEW signals from that point onward will be taken.
    # User decision May 1 — see CHANGELOG/HANDOFF.
    required = state.hl.margin_usd
    available = await state.hl.get_available_margin()
    if available is not None and available < required:
        log.warning(
            "DROPPED #%s %s — insufficient margin: $%.2f avail, $%.2f required "
            "(no backfill — signal lost by design)",
            trade_id, coin, available, required,
        )
        _push_activity(
            "blocked",
            f"💸 DROPPED {coin} #{trade_id} (margin $%.2f < $%.2f)" % (available, required),
            int(trade_id),
        )
        _safe_notify(
            f"💸 DROPPED #{trade_id} {coin} — insufficient margin "
            f"(${available:.2f} avail, ${required:.2f} needed)",
            "warning",
        )
        state.pending_trades.pop(int(trade_id), None)
        state.fire_refresh()
        return

    try:
        result = await state.hl.open_trade(
            trade_id=int(trade_id), portal_coin=coin, side=side,
            portal_entry=float(entry) if entry else None,
            portal_stop=float(stop) if stop else None,
            leverage=event.get("leverage"),
        )
    except HyperliquidValidationError as exc:
        _safe_notify(f"Open rejected #{trade_id}: {exc}", "negative")
        return
    except HyperliquidError as exc:
        _safe_notify(f"HL error #{trade_id}: {exc}", "negative")
        log.exception("open error #%s", trade_id)
        return
    except Exception as exc:
        _safe_notify(f"Open failed #{trade_id}: {exc}", "negative")
        log.exception("open failed #%s", trade_id)
        return

    # DB stores the PORTAL coin (bare, e.g. "SILVER") so is_coin_live() and
    # future portal events (which use the portal coin) find it. result.coin
    # now carries the HL order_name (e.g. "xyz:SILVER") — use it only for
    # user-facing display.
    # result.my_fill_price is the ACTUAL avg fill from HL; entry_price is the
    # slippage-padded limit we sent. Dashboard/PnL uses my_fill_price when set.
    db.insert_opened_trade(
        trade_id=int(trade_id), coin=coin, side=side,
        entry_price=result.entry_price, entry_sl=result.stop_price,
        size=result.size, margin=state.hl.margin_usd,
        leverage=event.get("leverage") or state.hl.default_leverage,
        caller=caller,
        my_fill_price=getattr(result, "my_fill_price", None),
    )
    db.add_live_trade(int(trade_id))
    state.pending_trades.pop(int(trade_id), None)

    # Notifier gets the real fill price when available (more accurate message).
    notifier_entry = getattr(result, "my_fill_price", None) or result.entry_price
    notifier.notify_opened(
        coin=result.coin, side=side, entry=notifier_entry,
        leverage=event.get("leverage") or state.hl.default_leverage,
        size=result.size, caller=caller, trade_id=int(trade_id),
        dry_run=result.dry_run,
    )
    _push_activity("opened", f"OPENED {result.coin} {side.upper()} #{trade_id}", int(trade_id))
    _safe_notify(f"Opened {result.coin} {side.upper()} (#{trade_id})", "positive")
    state.fire_refresh()


async def cancel_trade(trade_id: int) -> None:
    opened = db.get_opened_trade(int(trade_id))
    if opened is None or state.hl is None:
        return
    coin = opened["coin"]
    side = opened["side"]
    try:
        result = await state.hl.close_trade(
            trade_id=int(trade_id), portal_coin=coin, side=side,
        )
    except HyperliquidValidationError as exc:
        _safe_notify(f"Close rejected #{trade_id}: {exc}", "negative")
        # Still remove from DB if HL says nothing to close
        db.remove_live_trade(int(trade_id))
        state.fire_refresh()
        return
    except Exception as exc:
        _safe_notify(f"Close failed #{trade_id}: {exc}", "negative")
        log.exception("manual close failed #%s", trade_id)
        return

    _finalize_close(int(trade_id), opened, result, close_type="manual")
    _push_activity("closed_manual", f"CANCELLED {coin} #{trade_id}", int(trade_id))
    _safe_notify(f"Closed {coin} #{trade_id}", "warning")
    state.fire_refresh()


def dismiss_pending(trade_id: int) -> None:
    state.pending_trades.pop(int(trade_id), None)
    state.fire_refresh()


# ============================================================
# SECTION: Event router
# ============================================================

EVENT_HANDLERS = {
    "new_trade":   handle_new_trade,
    "stop_update": handle_stop_update,
    "tp_hit":      handle_tp_hit,
    "full_close":  handle_full_close,
}


async def route_event(event: dict) -> None:
    etype = event.get("type")
    trade_id = event.get("trade_id")
    handler = EVENT_HANDLERS.get(etype)
    if handler is None:
        log.debug("unrouted event type: %r", etype)
        return
    log.info("routing event: %s trade #%s", etype, trade_id)
    try:
        await handler(event)
    except Exception:
        log.exception("handler %s failed for event %r", etype, trade_id)


# ============================================================
# SECTION: Background tasks
# ============================================================

# Tunables for the supervisor + heartbeat. Read at on_startup time.
POLL_RESPAWN_BASE_DELAY  = 5.0         # seconds before first respawn
POLL_RESPAWN_MAX_DELAY   = 60.0        # cap
POLL_RESPAWN_RESET_AFTER = 300.0       # if a loop ran >5 min before dying, reset backoff
HEARTBEAT_INTERVAL_S     = float(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", 600))


async def portal_poll_loop() -> None:
    """One pass of portal polling. Returns/raises on any kind of exit; the
    supervisor below decides whether to respawn."""
    assert state.portal is not None
    try:
        async for event in state.portal.poll():
            await route_event(event)
    except PortalAuthError:
        # Don't suppress — let the supervisor see it and back off.
        log.error("portal poll loop: portal auth failure")
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("portal poll loop: uncaught exception")
        raise


async def portal_poll_supervisor() -> None:
    """Keep the portal poll loop alive forever.

    On Apr 28 we hit a silent-death failure: the poll loop returned without
    raising and was never respawned. Bot kept running for 11 hours processing
    zero portal events. Two protections:

      1. ANY exit (return OR exception, except CancelledError) triggers a
         respawn here.
      2. portal.poll() itself now raises a RuntimeError if its main loop
         exits without an explicit stop signal — see Fix B in portal.py.

    Backoff: starts at 5s, doubles up to 60s. Resets to 5s if a loop ran
    successfully for >5 minutes before failing.
    """
    backoff = POLL_RESPAWN_BASE_DELAY
    respawns = 0
    spawn_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log.info("portal poll supervisor active (first start at %s)", spawn_time)

    while True:
        loop_started_at = time.monotonic()
        loop_started_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            await portal_poll_loop()
            # Reaching here means the loop returned without an exception —
            # this is the silent-death signature. Treat as failure.
            exit_reason = "silent return (no exception)"
        except asyncio.CancelledError:
            log.info("portal poll supervisor: cancelled — exiting")
            raise
        except PortalAuthError as exc:
            exit_reason = f"PortalAuthError: {exc}"
        except Exception as exc:
            exit_reason = f"{type(exc).__name__}: {exc}"

        ran_for = time.monotonic() - loop_started_at
        if ran_for > POLL_RESPAWN_RESET_AFTER:
            backoff = POLL_RESPAWN_BASE_DELAY
        respawns += 1

        log.error(
            "portal poll supervisor: LOOP DIED — reason=%r ran_for=%.0fs "
            "respawn_count=%d sleeping=%.0fs",
            exit_reason, ran_for, respawns, backoff,
        )
        _push_activity(
            "stale",
            f"⚠ portal-poll respawn #{respawns} ({exit_reason[:40]})",
            None,
        )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, POLL_RESPAWN_MAX_DELAY)
        log.warning(
            "portal poll supervisor: RESPAWNING portal_poll_loop "
            "(respawn #%d at %s; previous loop started %s, ran %.0fs)",
            respawns,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            loop_started_ts, ran_for,
        )


async def heartbeat_loop() -> None:
    """Periodic 'still alive' ping to NOTIFY_WEBHOOK_URL.

    If you stop seeing these in your channel, the bot is dead — within
    HEARTBEAT_INTERVAL_S of the failure, not 11 hours later.

    No-op if NOTIFY_WEBHOOK_URL is unset (notifier.notify_heartbeat is silent).
    Includes time-since-last-portal-poll so a "alive but stuck" bot is also
    detectable from the heartbeat content.
    """
    interval = HEARTBEAT_INTERVAL_S
    log.info(
        "heartbeat loop active (interval=%.0fs, enabled=%s)",
        interval, notifier.is_enabled(),
    )
    # First ping after one full interval — gives startup logs space to settle.
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

        now_ms = int(time.time() * 1000)
        last_poll_ms = (
            state.portal.last_successful_poll_ms
            if state.portal is not None and hasattr(state.portal, "last_successful_poll_ms")
            else 0
        )
        secs_since_poll = (now_ms - last_poll_ms) / 1000.0 if last_poll_ms else None
        uptime_s = (now_ms - state.startup_time_ms) / 1000.0 if state.startup_time_ms else 0
        try:
            live_count = len(db.list_live_trades())
        except Exception:
            live_count = -1

        try:
            notifier.notify_heartbeat(
                uptime_seconds=uptime_s,
                seconds_since_last_poll=secs_since_poll,
                open_positions=live_count,
                dry_run=state.dry_run,
                auto_mode=state.auto_mode,
            )
            log.info(
                "heartbeat: uptime=%.0fs poll_age=%s open=%d auto_mode=%s",
                uptime_s,
                f"{secs_since_poll:.0f}s" if secs_since_poll is not None else "n/a",
                live_count, state.auto_mode,
            )
        except Exception:
            log.exception("heartbeat: send failed (swallowed)")


async def seed_closed_from_backlog() -> None:
    """Insert every trade_id that's closed in the portal backlog into hl_closed_trades.

    Rationale:
      The portal activity feed always returns recent history (up to ~50 events
      including trades from hours/days ago). On a FRESH DB, handle_new_trade's
      dedup check against hl_closed_trades finds nothing for backlog trade_ids
      — so auto-mode would attempt to re-enter every historical trade.

      This seeding pass fetches the current feed and marks every
      `trade_closed_*` event as pre-seeded in hl_closed_trades so the existing
      dedup path blocks them. Uses close_type='pre-seeded' to distinguish
      from real bot-driven closes (and get_stats / get_historic_trades filter
      these out).

      Idempotent: if the trade_id is already in hl_closed_trades we skip it.
    """
    if state.portal is None:
        return

    try:
        raw_events = await state.portal.get_activity_feed()
    except Exception:
        log.exception("seed_closed_from_backlog: could not fetch activity feed")
        return

    # Collect trade_ids from every close-style event in the feed.
    close_kinds = {
        "trade_closed", "trade_close", "full_close", "close",
        "position_closed", "stop_triggered", "sl_triggered",
        "auto_close", "stale_close", "cancelled", "canceled",
    }
    close_ids: dict[int, dict] = {}  # trade_id -> raw event
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or raw.get("eventType") or "").lower()
        if kind not in close_kinds:
            continue
        tid = raw.get("tradeId") or raw.get("trade_id")
        try:
            tid = int(tid) if tid is not None else None
        except (ValueError, TypeError):
            tid = None
        if tid is None:
            continue
        close_ids[tid] = raw

    if not close_ids:
        log.info("seed_closed_from_backlog: no close events in backlog")
        return

    seeded = 0
    skipped = 0
    for tid, raw in close_ids.items():
        if db.get_closed_trade(tid) is not None:
            skipped += 1
            continue
        coin = raw.get("coin") or "?"
        side = (raw.get("side") or "").lower() or "long"
        try:
            exit_price = float(
                raw.get("closePrice")
                or raw.get("exitPrice")
                or raw.get("fillPrice")
                or 0
            )
        except (ValueError, TypeError):
            exit_price = 0.0
        try:
            db.insert_closed_trade(
                trade_id=tid, coin=coin, side=side,
                entry_price=0.0,      # we don't know; pre-seed marker only
                exit_price=exit_price,
                size=0.0, trade_value=None, margin=None,
                fee=None, pnl=None,
                close_type="pre-seeded",
            )
            seeded += 1
        except Exception:
            log.exception("seed_closed_from_backlog: failed for #%s", tid)

    log.info(
        "seed_closed_from_backlog: %d pre-seeded, %d already in DB",
        seeded, skipped,
    )


async def reconcile_on_startup() -> None:
    """Compare HL open positions to DB live trades; log mismatches.

    SAFETY: open_positions() now returns None on a failed query (vs []
    for an actually-empty result). If we get None we MUST NOT clean up
    DB-only entries — doing so on a transient API failure was the May 1
    bug that wiped 4 live positions out of the bot's tracking.
    """
    if state.hl is None or state.dry_run:
        return
    try:
        hl_positions = await state.hl.open_positions()
    except Exception:
        log.exception("reconcile: failed to fetch HL positions")
        return

    if hl_positions is None:
        log.warning(
            "reconcile: HL position query failed (returned None) — "
            "skipping cleanup pass to avoid wiping live positions on "
            "a transient API blip"
        )
        return

    # HL reports positions using order_name (prefixed for HIP-3, e.g. "xyz:SILVER").
    # DB stores the portal coin (bare, e.g. "SILVER"). Normalize both to the
    # bare symbol by stripping any leading "dex:" prefix so the set-diff works.
    def _bare(name):
        if not isinstance(name, str):
            return name
        return name.split(":", 1)[1] if ":" in name else name

    hl_coins = {_bare(p["coin"]) for p in hl_positions if p.get("coin")}
    db_live = db.list_live_trades()
    db_coins = {_bare(t["coin"]) for t in db_live if t.get("coin")}

    only_hl = hl_coins - db_coins
    only_db = db_coins - hl_coins

    # Extra safety: if HL returned an EMPTY position list AND we have many
    # DB-only entries to clean, that's suspicious (possible silent failure
    # of user_state that returned {} instead of raising). Require at least
    # ONE coin-overlap OR an empty DB before we trust an "everything closed"
    # signal.
    if not hl_coins and db_coins:
        # We have DB live but HL says nothing. This could be legitimate
        # (user closed everything) OR a partial-failure mode. Be cautious:
        # log loudly so the user notices, and only clean coins that have
        # been gone for at least 2 consecutive reconcile passes (tracked
        # via an in-memory counter on AppState).
        prev = getattr(state, "_reconcile_empty_streak", 0) + 1
        state._reconcile_empty_streak = prev
        if prev < 2:
            log.warning(
                "reconcile: HL returned 0 positions but DB has %d live (%s). "
                "Waiting for a 2nd consecutive empty result before cleaning "
                "(streak=%d).",
                len(db_coins), sorted(db_coins), prev,
            )
            return
        log.warning(
            "reconcile: HL returned 0 positions for the %dnd consecutive "
            "pass — proceeding with cleanup of DB-only entries: %s",
            prev, sorted(db_coins),
        )
    else:
        # Reset streak as soon as HL acknowledges any position
        state._reconcile_empty_streak = 0

    if only_hl:
        log.warning(
            "startup sync: HL has positions not tracked in DB: %s "
            "(opened manually? — left untouched)",
            sorted(only_hl),
        )
    if only_db:
        log.warning(
            "startup sync: DB marks live but HL has no position: %s "
            "(cleaning DB)",
            sorted(only_db),
        )
        for t in db_live:
            if t.get("coin") in only_db and t.get("trade_id") is not None:
                db.remove_live_trade(int(t["trade_id"]))

    if not only_hl and not only_db:
        log.info("startup sync: HL and DB in sync (%d positions)", len(hl_coins))


# Periodic reconcile interval (seconds). Bounds the BLOCKED-window after a
# manual close: the same coin can be re-entered at most this many seconds
# after you close it on the HL UI, even if the portal hasn't broadcast a
# trade_closed event yet. Configurable via RECONCILE_INTERVAL_SECONDS env.
# Reconcile interval. Default tightened from 300s → 60s on May 1 so that
# manual changes on HL (closes, partial reductions, etc.) are reflected in
# the bot's DB within a minute instead of 5. Sub-second awareness needs the
# HL userEvents WS subscription (Phase 2).
RECONCILE_INTERVAL_S = float(os.environ.get("RECONCILE_INTERVAL_SECONDS", 60))

# Pending-queue drain interval. Trades that couldn't be opened (insufficient
# margin) are retried this often. Also fired immediately after every successful
# close (since closing frees margin).
PENDING_DRAIN_INTERVAL_S = float(os.environ.get("PENDING_DRAIN_INTERVAL_SECONDS", 60))


async def periodic_reconcile_loop() -> None:
    """Background task: re-runs reconcile_on_startup() every 5 minutes.

    Why: when a user closes a position manually on the HL UI, the bot's DB
    still flags that coin as live until the portal sends a trade_closed
    event (which can be hours later). During that window, new signals on
    the SAME coin hit the BLOCKED guard in handle_new_trade and never
    auto-execute. Periodic reconcile clears DB-only entries, bounding the
    BLOCKED window to RECONCILE_INTERVAL_S.

    Idempotent: reconcile_on_startup() only writes when there's a
    discrepancy, so calling it on every interval is cheap.
    """
    log.info(
        "periodic reconcile active (interval=%.0fs)", RECONCILE_INTERVAL_S
    )
    while True:
        try:
            await asyncio.sleep(RECONCILE_INTERVAL_S)
        except asyncio.CancelledError:
            return
        try:
            await reconcile_on_startup()
        except Exception:
            log.exception("periodic reconcile failed (will retry next tick)")


async def hl_change_reconciler() -> None:
    """Phase-2 real-time reconciler.

    The HL userEvents WS subscription (in HyperliquidClient._ws_loop)
    sets state.hl_change_event whenever ANY user-side change happens —
    a fill, a manual close on the HL UI, a manual SL adjustment, a
    liquidation, etc. This task awaits the event, debounces (collects
    events arriving within HL_CHANGE_DEBOUNCE_S of each other), then
    runs reconcile_on_startup() to update the bot's DB state.

    Net result: manual UI changes reflected in bot DB within ~2 seconds
    instead of the 60-second periodic-reconcile cycle.

    The 60s periodic reconcile remains as a safety net in case WS events
    are dropped or the user-events subscription fails silently.
    """
    debounce_s = float(os.environ.get("HL_CHANGE_DEBOUNCE_SECONDS", 2.0))
    log.info("hl-change reconciler active (debounce=%.1fs)", debounce_s)

    if state.hl_change_event is None:
        state.hl_change_event = asyncio.Event()

    while True:
        try:
            await state.hl_change_event.wait()
        except asyncio.CancelledError:
            return
        # Debounce: keep collecting events for `debounce_s` seconds before
        # firing the reconcile. A burst of fills (e.g. many partial fills
        # for one order) will all be coalesced into one reconcile pass.
        await asyncio.sleep(debounce_s)
        state.hl_change_event.clear()
        try:
            await reconcile_on_startup()
            log.info("hl-change: reconcile triggered by HL userEvents")
        except Exception:
            log.exception("hl-change reconcile failed")


# ============================================================
# SECTION: App lifecycle
# ============================================================

@app.on_startup
async def on_startup() -> None:
    log.info("starting Corgi Copy Trading Bot")
    db.init_db()

    # Lock in the startup time BEFORE any portal polling can begin.
    # Any new_trade event whose portal timestamp is older than this (minus
    # STALE_SLACK_MS) is treated as stale and cannot be auto- or manually
    # entered — prevents the "fresh DB replays backlog" footgun.
    import time as _time
    state.startup_time_ms = int(_time.time() * 1000)
    log.info("startup cutoff: %d ms (trades older than this → STALE)",
             state.startup_time_ms - STALE_SLACK_MS)

    # FORCE_ENTER_TIDS — comma-separated trade IDs that bypass the STALE
    # check. Use this to take a legitimate but old call that was missed
    # during downtime. Example: FORCE_ENTER_TIDS=665,666
    fe_raw = os.environ.get("FORCE_ENTER_TIDS", "").strip()
    if fe_raw:
        for piece in fe_raw.split(","):
            piece = piece.strip()
            if piece.isdigit():
                state.force_enter_tids.add(int(piece))
        if state.force_enter_tids:
            log.warning(
                "FORCE_ENTER_TIDS active for: %s — these will bypass STALE",
                sorted(state.force_enter_tids),
            )

    state.hl = HyperliquidClient()
    state.dry_run = state.hl.dry_run

    # Phase-2 wiring: HL userEvents WS → state.hl_change_event → debounced reconcile.
    # Set up the asyncio.Event before start_price_feed (which spawns the WS task
    # that will start setting it as messages arrive).
    state.hl_change_event = asyncio.Event()
    def _on_hl_user_change(channel: str, data: dict) -> None:
        # Called from the WS task on the same event loop — safe to set Event.
        if state.hl_change_event is not None:
            state.hl_change_event.set()
    state.hl.set_user_change_callback(_on_hl_user_change)

    await state.hl.start_price_feed()

    state.portal = PortalClient()
    await state.portal.start()

    # Pre-seed hl_closed_trades with every trade_id that already has a close
    # event in the current activity-feed backlog. Combined with the dedup
    # check in handle_new_trade, this prevents the fresh-DB replay problem
    # even for historical trades that aren't covered by the startup cutoff.
    await seed_closed_from_backlog()

    await reconcile_on_startup()

    # Supervisor wraps portal_poll_loop and respawns it on ANY exit
    # (including silent return — see Apr 28 outage).
    state.portal_task = asyncio.create_task(
        portal_poll_supervisor(), name="portal-poll-supervisor"
    )
    state.heartbeat_task = asyncio.create_task(
        heartbeat_loop(), name="heartbeat"
    )
    state.reconcile_task = asyncio.create_task(
        periodic_reconcile_loop(), name="periodic-reconcile"
    )
    # Phase-2 reconciler — fires reconcile within ~2s of any HL change
    # observed via the userEvents WS subscription (debounced).
    state.hl_change_task = asyncio.create_task(
        hl_change_reconciler(), name="hl-change-reconciler"
    )

    log.info(
        "ready — dry_run=%s testnet=%s auto_mode=%s",
        state.dry_run,
        state.hl.testnet if state.hl else "?",
        state.auto_mode,
    )
    if state.auto_mode and not state.dry_run:
        log.warning(
            "⚠️  AUTO MODE ON + DRY_RUN=false — real orders will be placed "
            "automatically on every whitelisted new_trade event"
        )


@app.on_shutdown
async def on_shutdown() -> None:
    log.info("shutting down")
    for task_name, task in (
        ("portal_task", state.portal_task),
        ("heartbeat_task", state.heartbeat_task),
        ("reconcile_task", state.reconcile_task),
        ("hl_change_task", state.hl_change_task),
    ):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    if state.hl is not None:
        await state.hl.stop_price_feed()
    if state.portal is not None:
        await state.portal.close()


# ============================================================
# SECTION: UI
# ============================================================

@ui.page("/")
def index() -> None:
    ui.add_head_html(
        "<style>"
        ".trade-card { border-radius: 10px; padding: 14px; "
        "  border: 1px solid #2d3748; background: #1a202c; color: #e2e8f0; }"
        ".trade-card.blocked { opacity: 0.55; border-color: #a68300; }"
        ".stat-pill { background: #1a202c; color: #e2e8f0; padding: 8px 14px; "
        "  border-radius: 8px; font-weight: 600; }"
        ".live-dot { color: #48bb78; font-size: 18px; }"
        ".dry-banner { background: #744210; color: #fffbea; padding: 8px 14px; "
        "  border-radius: 6px; font-weight: 700; }"
        "</style>"
    )

    with ui.header().classes("items-center justify-between"):
        ui.label("Corgi Copy Trading Bot").classes("text-lg font-bold")
        with ui.row().classes("items-center gap-3"):
            auto_switch = ui.switch("Auto Mode", value=state.auto_mode)

            def _toggle_auto(e):
                state.auto_mode = bool(e.value)
                log.info("auto-mode → %s", state.auto_mode)
                ui.notify(
                    f"Auto mode {'ON' if state.auto_mode else 'OFF'}",
                    type="info",
                )
            auto_switch.on("update:model-value", _toggle_auto)
            if state.dry_run:
                ui.label("DRY RUN").classes("dry-banner")

    with ui.row().classes("w-full no-wrap gap-4 items-start"):
        # =======================  MAIN COLUMN  ========================
        with ui.column().classes("flex-grow gap-4"):

            # ---- stats header ----
            stats_pnl = ui.label().classes("stat-pill")
            stats_win = ui.label().classes("stat-pill")
            stats_open = ui.label().classes("stat-pill")
            stats_fees = ui.label().classes("stat-pill")
            stats_row_container = ui.row().classes("gap-3")
            with stats_row_container:
                pass

            def refresh_stats() -> None:
                try:
                    s = db.get_stats()
                except Exception:
                    log.exception("stats refresh failed")
                    return
                stats_pnl.text = f"PnL: {_fmt_pnl(s['total_pnl'])}"
                stats_win.text = (
                    f"Win Rate: {s['win_rate']*100:.1f}%  "
                    f"({s['wins']}/{s['total_closed']})"
                )
                stats_open.text = f"Open: {s['open_count']}"
                stats_fees.text = f"Fees: ${s['total_fees']:.2f}"

            # Put the four stat pills into the row
            with stats_row_container:
                for w in (stats_pnl, stats_win, stats_open, stats_fees):
                    w.move(stats_row_container)

            ui.separator()

            # ---- active trade cards ----
            ui.label("Active Trades").classes("text-base font-semibold mt-2")
            cards_container = ui.column().classes("w-full gap-3")

            @ui.refreshable
            def render_cards() -> None:
                cards_container.clear()
                with cards_container:
                    live = db.list_live_trades()
                    live_ids = {int(t["trade_id"]) for t in live}

                    if not live and not state.pending_trades:
                        ui.label("— no active trades —").classes("text-sm opacity-60")

                    # LIVE cards
                    for t in live:
                        _build_live_card(t)

                    # PENDING cards
                    for tid, evt in list(state.pending_trades.items()):
                        if int(tid) in live_ids:
                            state.pending_trades.pop(int(tid), None)
                            continue
                        _build_pending_card(int(tid), evt)

            state.register_refresh(render_cards.refresh)
            render_cards()

            ui.separator()

            # ---- historic trades table ----
            ui.label("Historic Trades").classes("text-base font-semibold mt-2")
            history_columns = [
                {"name": "closed_at", "label": "Date", "field": "closed_at"},
                {"name": "coin",      "label": "Coin", "field": "coin"},
                {"name": "side",      "label": "Side", "field": "side"},
                {"name": "caller",    "label": "Caller", "field": "caller"},
                {"name": "entry",     "label": "Entry", "field": "entry"},
                {"name": "exit",      "label": "Exit",  "field": "exit"},
                {"name": "pnl",       "label": "PnL",   "field": "pnl"},
                {"name": "close_type","label": "Type",  "field": "close_type"},
            ]
            history_table = ui.table(
                columns=history_columns, rows=[], row_key="id",
            ).classes("w-full")

            def refresh_history() -> None:
                try:
                    rows = db.get_historic_trades(limit=200)
                except Exception:
                    log.exception("history refresh failed")
                    return
                history_table.rows = [
                    {
                        "id":         r["id"],
                        "closed_at":  r["closed_at"],
                        "coin":       r["coin"],
                        "side":       (r["side"] or "").upper(),
                        "caller":     r.get("caller") or "—",
                        "entry":      _fmt_price(r["entry_price"]),
                        "exit":       _fmt_price(r["exit_price"]),
                        "pnl":        _fmt_pnl(r["pnl"]),
                        "close_type": r["close_type"],
                    }
                    for r in rows
                ]
                history_table.update()

        # =======================  SIDEBAR  =============================
        with ui.column().classes("w-80 gap-2"):
            ui.label("Activity Feed").classes("text-base font-semibold")
            feed_container = ui.column().classes(
                "w-full gap-1 overflow-auto"
            ).style("max-height: 80vh;")

            @ui.refreshable
            def render_feed() -> None:
                feed_container.clear()
                with feed_container:
                    if not state.activity_feed:
                        ui.label("— no events yet —").classes("text-xs opacity-60")
                        return
                    for evt in list(state.activity_feed)[:60]:
                        color = _feed_color(evt["kind"])
                        with ui.row().classes("w-full no-wrap gap-2 items-baseline"):
                            ui.label(evt["at"]).classes("text-xs opacity-60")
                            ui.label(evt["text"]).classes(f"text-xs {color}")

            state.register_refresh(render_feed.refresh)
            render_feed()

    # ---- periodic updates ----
    refresh_stats()
    refresh_history()

    def tick_fast() -> None:
        # update live prices / PnL labels on existing cards
        _tick_card_prices()

    def tick_slow() -> None:
        refresh_stats()
        refresh_history()

    ui.timer(1.0, tick_fast)
    ui.timer(3.0, tick_slow)


def _feed_color(kind: str) -> str:
    return {
        "new_trade":      "text-blue-400",
        "opened":         "text-green-400",
        "tp_hit":         "text-teal-300",
        "stop_update":    "text-yellow-300",
        "close":          "text-gray-300",
        "closed_manual":  "text-orange-300",
        "sl_triggered":   "text-red-400",
        "blocked":        "text-yellow-500",
        "stale":          "text-orange-400",
    }.get(kind, "text-gray-400")


# ============================================================
# SECTION: Card builders — hold refs to mid/pnl labels for live ticks
# ============================================================

# Map trade_id -> {mid_label, pnl_label, hl_coin, entry, side, size}
_live_card_refs: dict[int, dict] = {}


def _build_live_card(t: dict) -> None:
    trade_id = int(t["trade_id"])
    coin = t.get("coin") or "?"
    side = (t.get("side") or "long").lower()
    # PnL basis: prefer the real HL fill price when we have it; fall back to
    # the limit price only for older rows (pre-migration) or reconcile failures.
    fill_px = t.get("my_fill_price")
    limit_px = float(t.get("entry_price") or 0)
    pnl_basis = float(fill_px) if fill_px not in (None, "") else limit_px
    stop = t.get("entry_sl")
    size = float(t.get("size") or 0)
    caller = t.get("caller") or "?"
    hl_coin = hl_symbol_for(coin)

    with ui.card().classes("trade-card w-full"):
        with ui.row().classes("w-full justify-between items-center"):
            ui.label(
                f"{coin} {side.upper()} — {caller} — #{trade_id}"
            ).classes("font-bold")
            try:
                ui.html('<span class="live-dot">●</span> LIVE', sanitize=False)
            except TypeError:
                # Older NiceGUI without the sanitize kwarg
                ui.html('<span class="live-dot">●</span> LIVE')

        with ui.row().classes("gap-6 text-sm mt-1"):
            # Display the real fill when available; limit price is just the
            # worst-case cushion — not useful to surface unless fill is missing.
            if fill_px not in (None, ""):
                ui.label(f"Fill: {_fmt_price(pnl_basis)}")
            else:
                ui.label(f"Entry*: {_fmt_price(limit_px)}").tooltip(
                    "Limit price (no fill reconciled) — PnL may be slippage-biased"
                )
            ui.label(f"SL: {_fmt_price(stop)}")
            ui.label(f"Size: {size:g}")

        mid_label = ui.label(f"Mid: —").classes("text-sm")
        pnl_label = ui.label(f"PnL: —").classes("text-sm font-semibold")

        _live_card_refs[trade_id] = {
            "mid_label": mid_label, "pnl_label": pnl_label,
            "hl_coin": hl_coin, "entry": pnl_basis, "side": side, "size": size,
        }

        with ui.row().classes("justify-end w-full mt-2"):
            ui.button(
                "Cancel",
                on_click=lambda tid=trade_id: asyncio.create_task(cancel_trade(tid)),
                color="red",
            )


def _build_pending_card(trade_id: int, evt: dict) -> None:
    coin = evt.get("coin") or "?"
    side = (evt.get("side") or "long").lower()
    entry = evt.get("entry_price")
    stop = evt.get("stop_loss")
    caller = evt.get("caller") or "?"
    blocked = db.is_coin_live(coin)
    is_stale = bool(evt.get("stale"))

    classes = "trade-card w-full"
    if blocked or is_stale:
        classes += " blocked"

    with ui.card().classes(classes):
        with ui.row().classes("w-full justify-between items-center"):
            ui.label(
                f"{coin} {side.upper()} — {caller} — #{trade_id}"
            ).classes("font-bold")
            # STALE takes priority over BLOCKED in the right-side badge
            if is_stale:
                ui.label("STALE (posted before bot start — entry disabled)").classes(
                    "text-orange-400 font-semibold"
                )
            elif blocked:
                ui.label(f"BLOCKED ({coin} already live)").classes("text-yellow-400")

        with ui.row().classes("gap-6 text-sm mt-1"):
            ui.label(f"Entry: {_fmt_price(entry)}")
            ui.label(f"SL: {_fmt_price(stop)}")

        with ui.row().classes("justify-end w-full mt-2 gap-2"):
            ui.button(
                "Dismiss",
                on_click=lambda tid=trade_id: dismiss_pending(tid),
            ).props("flat")
            enter_btn = ui.button(
                "Enter",
                on_click=lambda tid=trade_id: asyncio.create_task(enter_trade(tid)),
                color="primary",
            )
            if blocked or is_stale:
                enter_btn.disable()


def _tick_card_prices() -> None:
    if state.hl is None or not _live_card_refs:
        return
    # Purge refs for trades no longer live
    live_ids = {int(t["trade_id"]) for t in db.list_live_trades()}
    stale = [tid for tid in _live_card_refs if tid not in live_ids]
    for tid in stale:
        _live_card_refs.pop(tid, None)

    for tid, ref in list(_live_card_refs.items()):
        try:
            mid = state.hl.get_mid(ref["hl_coin"])
            if mid is None:
                continue
            ref["mid_label"].text = f"Mid: {_fmt_price(mid)}"
            entry = ref["entry"]
            size = ref["size"]
            if entry and size:
                direction = 1 if ref["side"] in ("long", "buy") else -1
                pnl = (mid - entry) * size * direction
                color = (
                    "text-green-400" if pnl > 0
                    else ("text-red-400" if pnl < 0 else "text-gray-300")
                )
                ref["pnl_label"].text = f"PnL: {_fmt_pnl(pnl)}"
                ref["pnl_label"].classes(replace=f"text-sm font-semibold {color}")
        except Exception:
            log.debug("tick update failed for #%s", tid, exc_info=True)


# ============================================================
# SECTION: Run
# ============================================================

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="Corgi Copy Trading Bot",
        port=int(os.environ.get("PORT", 8080)),
        host=os.environ.get("HOST", "0.0.0.0"),
        reload=False,
        show=False,
        dark=True,
    )
