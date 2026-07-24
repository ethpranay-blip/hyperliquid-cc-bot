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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from nicegui import app, ui

from app import db
from app import notifier
from app.hyperliquid_client import (
    HyperliquidClient, HyperliquidError, HyperliquidValidationError,
    hl_symbol_for, is_k_coin, MIN_NOTIONAL_USD,
)
from app.execution import (
    LevelSlippageExceeded, get_sl_slip_pct, get_tp_slip_pct,
)
from app.performance import (
    reconcile as _perf_reconcile,
    summarize as _perf_summarize,
    cumulative_series as _perf_cumulative,
    aggregate_portal_trades as _perf_aggregate_portal,
    STATUS_COPIED_CLOSED, STATUS_MISSED, STATUS_COPIED_OPEN,
    STATUS_COPIED_ORPHAN, STATUS_BOT_ONLY,
)
from app.portal import PortalClient, PortalAuthError
from app.trailing import compute_trailed_stop
from app.adoption import build_open_trade_index
from app.auth import auth_enabled, password_ok


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
        # maxlen bounds the WebSocket state-sync payload (see DASH_FEED_ROWS).
        self.activity_feed: deque[dict] = deque(maxlen=80)
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

# How long a resting GTC entry (parked at the caller's level after the live
# mid drifted past the slippage cap) waits for a fill before being cancelled.
RESTING_ENTRY_TTL_MIN = float(os.environ.get("RESTING_ENTRY_TTL_MINUTES", 60))

# Dashboard render caps. NiceGUI syncs the whole page's element state to the
# browser over a single WebSocket message; once it exceeds the transport limit
# the client can never (re)connect ("Message too long" → the page freezes).
# These bound the biggest row-sets so the sync stays small. Env-tunable.
DASH_HISTORY_ROWS = int(os.environ.get("DASH_HISTORY_ROWS", 40))
DASH_FEED_ROWS = int(os.environ.get("DASH_FEED_ROWS", 40))
PERF_TABLE_ROWS = int(os.environ.get("PERF_TABLE_ROWS", 60))



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
    if db.get_resting_entry(int(trade_id)) is not None:
        log.info("new_trade #%s already has a resting entry — skipping", trade_id)
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
        notifier.notify_skipped(
            coin=coin, side=side, reason="stale", caller=caller,
            trade_id=int(trade_id), detail="posted before bot start",
        )
        db.insert_skipped_trade(
            trade_id=int(trade_id), coin=coin, side=side, caller=caller,
            reason="stale", detail="posted before bot start",
        )
        return

    # Auto-mode: open immediately if coin not already live (or resting)
    if state.auto_mode:
        if db.is_coin_live(coin) or db.is_coin_resting(coin):
            log.info("auto-mode: %s already live/resting → BLOCKED for #%s", coin, trade_id)
            _push_activity("blocked", f"BLOCKED {coin} (already live/resting) #{trade_id}", trade_id)
            notifier.notify_skipped(
                coin=coin, side=side, reason="blocked_coin_live",
                caller=caller, trade_id=int(trade_id),
                detail=f"{coin} already has an open position or resting entry",
            )
            db.insert_skipped_trade(
                trade_id=int(trade_id), coin=coin, side=side, caller=caller,
                reason="blocked_coin_live",
                detail=f"{coin} already has an open position or resting entry",
            )
            return
        log.info("auto-mode: entering #%s", trade_id)
        await enter_trade(trade_id)


async def _auto_trail_stop_after_tp(
    trade_id: int, coin: str, side: str, tp_num: Optional[int], opened: dict
) -> None:
    """
    Automatically trail the stop after a TP books, per Corgi Calls rules:
    - TP1/TP2 hit → move stop to breakeven (the ACTUAL fill price)
    - TP3 hit     → move stop to TP1 price
    - TP4 hit     → move stop to TP2 price

    Three correctness guards, all added after the May 22 full-close incident:

    1. BREAKEVEN = my_fill_price, NOT entry_price. entry_price stores the
       slippage-padded LIMIT the bot sent (mid×1.05 long / mid×0.95 short).
       Using it placed the SL on the wrong side of the fill → instant
       stop-out the moment TP1 booked (VVV short, BTC short — May 22).

    2. RATCHET (compute_trailed_stop): never loosen. Keep the tighter of
       {tp target, current stop}. So a profit-locking stop the trader already
       set (e.g. short @ $15.75) survives instead of being dragged back to BE.

    3. VALIDITY GUARD: refuse to place an SL on the wrong side of the live mid
       (it would trigger instantly). Belt-and-suspenders on top of (1).

    The portal usually omits tp_num; we infer it from the recorded TP count
    (db.insert_tp_update runs before this in handle_tp_hit, so the count IS
    this TP's ordinal).
    """
    if state.hl is None:
        log.warning("AUTO TRAILING: skip #%s — HL client not ready", trade_id)
        return

    # Infer tp_num when the portal omits it.
    if tp_num is None:
        inferred = db.get_tp_update_count(int(trade_id))
        if inferred < 1:
            log.warning(
                "AUTO TRAILING: skip #%s — tp_num None and no TP rows yet "
                "(insert_tp_update may not have run)", trade_id,
            )
            return
        log.info(
            "AUTO TRAILING: portal omitted tp_num for #%s — inferred=%d "
            "(from hl_tp_updates row count)", trade_id, inferred,
        )
        tp_num = inferred

    # Guard 1: breakeven basis = REAL fill, not the limit price.
    fill_price = opened.get("my_fill_price")
    if fill_price in (None, ""):
        fill_price = opened.get("entry_price")
        log.warning(
            "AUTO TRAILING: #%s has no my_fill_price — falling back to "
            "entry_price (limit) for BE; trail may be slightly off", trade_id,
        )
    if fill_price in (None, ""):
        log.warning("AUTO TRAILING: skip #%s — no fill or entry price in DB", trade_id)
        return
    fill_price = float(fill_price)

    is_long = side.lower() in ("long", "buy")
    current_stop = db.get_current_stop(int(trade_id))
    tp1_price = db.get_tp_price_from_history(int(trade_id), tp_num=1)
    tp2_price = db.get_tp_price_from_history(int(trade_id), tp_num=2)

    # Guard 2: rule target + never-loosen ratchet (pure, tested in app/trailing).
    new_stop, reason = compute_trailed_stop(
        is_long=is_long, tp_num=int(tp_num), fill_price=fill_price,
        current_stop=current_stop, tp1_price=tp1_price, tp2_price=tp2_price,
    )
    if new_stop is None:
        log.warning("AUTO TRAILING: skip #%s — %s", trade_id, reason)
        return

    # Guard 3: never place an SL on the wrong side of the live mid (would
    # trigger instantly). If mid is unavailable, proceed — the my_fill_price
    # basis already prevents the original failure mode.
    try:
        mid = await state.hl.get_price_for_pricing(coin)
    except Exception:
        mid = None
    if mid is not None and mid > 0:
        if is_long and new_stop >= mid:
            log.warning(
                "AUTO TRAILING: skip #%s — long SL %.6f >= mid %.6f "
                "(would trigger instantly); leaving existing stop in place",
                trade_id, new_stop, mid,
            )
            return
        if (not is_long) and new_stop <= mid:
            log.warning(
                "AUTO TRAILING: skip #%s — short SL %.6f <= mid %.6f "
                "(would trigger instantly); leaving existing stop in place",
                trade_id, new_stop, mid,
            )
            return

    try:
        log.info(
            "AUTO TRAILING STOP: #%s %s - %s (new_stop=%.6f, was=%s, fill=%.6f)",
            trade_id, coin, reason, new_stop,
            f"{current_stop:.6f}" if current_stop is not None else "none",
            fill_price,
        )
        await state.hl.update_stop(
            trade_id=int(trade_id), portal_coin=coin, side=side,
            new_portal_stop=new_stop, portal_entry=fill_price,
        )
        db.insert_sl_update(
            trade_id=int(trade_id), old_stop=current_stop, new_stop=new_stop,
        )
        _push_activity(
            "stop_update",
            f"🔒 AUTO SL→{_fmt_price(new_stop)} {coin} #{trade_id} ({reason})",
            int(trade_id),
        )
        notifier.notify_sl_moved(
            coin=coin, side=side, old_stop=current_stop, new_stop=new_stop,
            reason=reason, trade_id=int(trade_id), dry_run=state.dry_run,
        )
        log.info("AUTO TRAILING: SL updated #%s → %.6f (%s)", trade_id, new_stop, reason)
    except Exception as exc:
        log.exception("AUTO TRAILING: update_stop failed #%s", trade_id)
        # place-before-cancel → the prior stop is still in force; alert that
        # the TRAIL didn't apply, but the position remains protected.
        notifier.notify_sl_failed(
            coin=coin, side=side, intended_stop=new_stop, reason=str(exc),
            trade_id=int(trade_id), protected=True, dry_run=state.dry_run,
        )


async def handle_stop_update(event: dict) -> None:
    trade_id = event.get("trade_id")
    new_stop = event.get("new_stop")
    if trade_id is None or new_stop is None:
        return

    # Caller moved their SL while our bracket is still resting (unfilled):
    # cancel and re-place the bracket with the updated SL. Funds-safe even if
    # the re-place fails — an unfilled entry means no position to protect;
    # worst case we just miss the trade and record the skip.
    resting = db.get_resting_entry(int(trade_id))
    if resting is not None and not db.get_live_trade(int(trade_id)):
        if state.hl is None:
            return
        try:
            await state.hl.cancel_resting_entry(
                trade_id=int(trade_id), portal_coin=resting["coin"],
            )
        except Exception:
            log.exception("resting SL-update: cancel failed #%s", trade_id)
        db.remove_resting_entry(int(trade_id))
        sizing = db.get_sizing_settings(default_margin_usd=state.hl.margin_usd)
        available = await state.hl.get_available_margin()
        placed = await _place_resting_entry_for(
            trade_id=int(trade_id), coin=resting["coin"],
            side=resting["side"], caller=resting.get("caller"),
            entry=resting["entry_px"], stop=float(new_stop),
            leverage=None,
            mode=sizing["sizing_mode"], margin_usd=sizing["margin_usd"],
            risk_usd=sizing["risk_usd"], available=available,
        )
        if placed:
            log.info(
                "resting bracket #%s re-placed with caller's new SL %s",
                trade_id, new_stop,
            )
        else:
            notifier.notify_skipped(
                coin=resting["coin"], side=resting.get("side"),
                reason="resting_cancelled", caller=resting.get("caller"),
                trade_id=int(trade_id),
                detail=f"could not re-place bracket after SL update to {new_stop}",
            )
            db.insert_skipped_trade(
                trade_id=int(trade_id), coin=resting["coin"],
                side=resting.get("side"), caller=resting.get("caller"),
                reason="resting_cancelled",
                detail=f"re-place failed after SL update to {new_stop}",
            )
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
        notifier.notify_sl_moved(
            coin=coin, side=side, old_stop=opened.get("entry_sl"),
            new_stop=float(new_stop), reason="portal update",
            trade_id=int(trade_id), dry_run=state.dry_run,
        )
    except Exception as exc:
        log.exception("update_stop failed #%s", trade_id)
        # place-before-cancel → prior stop still protecting; alert the move failed.
        notifier.notify_sl_failed(
            coin=coin, side=side, intended_stop=float(new_stop), reason=str(exc),
            trade_id=int(trade_id), protected=True, dry_run=state.dry_run,
        )


async def handle_tp_hit(event: dict) -> None:
    trade_id = event.get("trade_id")
    size_pct = event.get("size_pct")
    tp_price = event.get("tp_price")
    tp_num = event.get("tp_num")
    log.info(
        "tp_hit fields: trade_id=%s tp_num=%r size_pct=%r tp_price=%r",
        trade_id, tp_num, size_pct, tp_price,
    )
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

    # Caller-posted TP price → used as the strict slippage cap reference.
    tp_level = float(tp_price) if tp_price is not None else None

    try:
        if float(size_pct) >= 100:
            # Treated as full close
            result = await state.hl.close_trade(
                trade_id=int(trade_id), portal_coin=coin, side=side,
                target_level=tp_level,
            )
            _finalize_close(int(trade_id), opened, result, close_type="automatic")
        else:
            result = await state.hl.partial_tp(
                trade_id=int(trade_id), portal_coin=coin, side=side,
                size_pct=float(size_pct), target_level=tp_level,
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

    except LevelSlippageExceeded as exc:
        log.warning(
            "TP SLIPPED #%s %s — mid=%g vs tp_level=%g drift=%.2f%% cap=%.2f%% "
            "(partial NOT booked; position stays full size)",
            trade_id, coin, exc.mid, exc.level,
            exc.drift_pct * 100, exc.slip_pct * 100,
        )
        _push_activity(
            "blocked",
            f"🎯 TP SLIPPED {coin} #{trade_id} (mid {_fmt_price(exc.mid)} vs "
            f"tp {_fmt_price(exc.level)}, drift {exc.drift_pct*100:.2f}%)",
            int(trade_id),
        )
        notifier.notify_skipped(
            coin=coin, side=side, reason="tp_slipped",
            caller=opened.get("caller"), trade_id=int(trade_id),
            detail=(
                f"mid {_fmt_price(exc.mid)} vs TP {_fmt_price(exc.level)} "
                f"({exc.drift_pct*100:.2f}% drift > {exc.slip_pct*100:.2f}% cap) "
                f"— partial not booked"
            ),
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

    # Caller closed a trade whose resting bracket never filled → withdraw it.
    resting = db.get_resting_entry(int(trade_id))
    if resting is not None and not db.get_live_trade(int(trade_id)):
        try:
            if state.hl is not None:
                await state.hl.cancel_resting_entry(
                    trade_id=int(trade_id), portal_coin=resting["coin"],
                )
        except Exception:
            log.exception("failed to cancel resting bracket #%s", trade_id)
        db.remove_resting_entry(int(trade_id))
        _push_activity(
            "resting",
            f"🕒 RESTING WITHDRAWN {resting['coin']} #{trade_id} (caller closed)",
            int(trade_id),
        )
        notifier.notify_skipped(
            coin=resting["coin"], side=resting.get("side"),
            reason="resting_cancelled", caller=resting.get("caller"),
            trade_id=int(trade_id),
            detail="caller closed the trade before our resting entry filled",
        )
        db.insert_skipped_trade(
            trade_id=int(trade_id), coin=resting["coin"],
            side=resting.get("side"), caller=resting.get("caller"),
            reason="resting_cancelled",
            detail="caller closed before entry filled",
        )
        state.fire_refresh()
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

    # Caller-posted exit price → strict slippage cap reference.
    close_level = event.get("exit_price")
    try:
        close_level = float(close_level) if close_level is not None else None
    except (ValueError, TypeError):
        close_level = None

    # Tighter cap when an SL fired (precision matters; HL's own SL trigger
    # is the safety net if we miss). More lenient for vob's manual exit
    # (a winner at 1% off the called exit is still a winner).
    close_slip = get_sl_slip_pct() if stop_triggered else get_tp_slip_pct()

    try:
        result = await state.hl.close_trade(
            trade_id=int(trade_id), portal_coin=coin, side=side,
            target_level=close_level, slip_pct=close_slip,
        )
        _finalize_close(
            int(trade_id), opened, result,
            close_type="stop_triggered" if stop_triggered else "automatic",
        )
    except LevelSlippageExceeded as exc:
        # Mid moved past the close-cap. Position remains OPEN. HL's own SL
        # trigger (placed at open) is the safety net for runaway losses.
        log.warning(
            "CLOSE SLIPPED #%s %s — mid=%g vs close_level=%g drift=%.2f%% cap=%.2f%% "
            "(position remains open; HL-side SL trigger is the safety net)",
            trade_id, coin, exc.mid, exc.level,
            exc.drift_pct * 100, exc.slip_pct * 100,
        )
        _push_activity(
            "blocked",
            f"🎯 CLOSE SLIPPED {coin} #{trade_id} (mid {_fmt_price(exc.mid)} vs "
            f"close {_fmt_price(exc.level)}, drift {exc.drift_pct*100:.2f}%)",
            int(trade_id),
        )
        notifier.notify_skipped(
            coin=coin, side=side, reason="close_slipped",
            caller=opened.get("caller"), trade_id=int(trade_id),
            detail=(
                f"mid {_fmt_price(exc.mid)} vs close {_fmt_price(exc.level)} "
                f"({exc.drift_pct*100:.2f}% drift > {exc.slip_pct*100:.2f}% cap) "
                f"— position still open"
            ),
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


async def _place_resting_entry_for(
    *,
    trade_id: int,
    coin: str,
    side: str,
    caller: Optional[str],
    entry: Optional[float],
    stop: Optional[float],
    leverage: Optional[float],
    mode: str,
    margin_usd: float,
    risk_usd: float,
    available: Optional[float],
) -> bool:
    """Park a GTC bracket at the caller's entry instead of skipping forever.

    Returns True if the bracket was placed and recorded (caller stops there);
    False means resting isn't possible for this signal (no SL, k-coin, HL
    rejection) and the normal skip path should proceed. Fill detection is the
    existing reconcile/adoption path; expiry/cancel is the sweeper.
    """
    if state.hl is None or entry is None or stop is None or is_k_coin(coin):
        return False
    try:
        res = await state.hl.place_resting_entry(
            trade_id=int(trade_id), portal_coin=coin, side=side,
            entry_px=float(entry), sl_px=float(stop), leverage=leverage,
            sizing_mode=mode, margin_usd=margin_usd, risk_usd=risk_usd,
            available_margin=available,
        )
    except HyperliquidValidationError as exc:
        log.info("resting entry not possible #%s %s: %s", trade_id, coin, exc)
        return False
    except Exception:
        log.exception("resting entry placement failed #%s %s", trade_id, coin)
        return False

    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=RESTING_ENTRY_TTL_MIN)
    ).isoformat(timespec="seconds")
    db.insert_resting_entry(
        trade_id=int(trade_id), coin=coin, side=side, caller=caller,
        entry_px=res["entry_px"], sl_px=res["sl_px"], size=res["size"],
        expires_at=expires_at,
    )
    log.info(
        "RESTING ENTRY #%s %s %s @ %s (sl=%s sz=%s, expires %s)",
        trade_id, coin, side, res["entry_px"], res["sl_px"], res["size"],
        expires_at,
    )
    _push_activity(
        "resting",
        f"🕒 RESTING {coin} {side.upper()} @ {_fmt_price(res['entry_px'])} "
        f"#{trade_id} (waiting ≤{RESTING_ENTRY_TTL_MIN:g}m)",
        int(trade_id),
    )
    notifier.notify_resting(
        coin=coin, side=side, entry=res["entry_px"], sl=res["sl_px"],
        ttl_minutes=RESTING_ENTRY_TTL_MIN, trade_id=int(trade_id),
        dry_run=state.dry_run,
    )
    state.pending_trades.pop(int(trade_id), None)
    state.fire_refresh()
    return True


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

    if db.is_coin_live(coin) or db.is_coin_resting(coin):
        _safe_notify(f"BLOCKED: {coin} already live or resting", "warning")
        notifier.notify_skipped(
            coin=coin, side=side, reason="blocked_coin_live",
            caller=caller, trade_id=int(trade_id),
            detail=f"{coin} already has an open position or resting entry",
        )
        db.insert_skipped_trade(
            trade_id=int(trade_id), coin=coin, side=side, caller=caller,
            reason="blocked_coin_live",
            detail=f"{coin} already has an open position or resting entry",
        )
        return
    if state.hl is None:
        _safe_notify("HL client not ready", "negative")
        return

    # ── SIZING MODE (live, dashboard-editable) ──
    sizing = db.get_sizing_settings(default_margin_usd=state.hl.margin_usd)
    mode = sizing["sizing_mode"]
    margin_usd = sizing["margin_usd"]
    risk_usd = sizing["risk_usd"]

    # ── PRE-FLIGHT MARGIN CHECK ──
    # Check withdrawable BEFORE submitting. In fixed_margin mode we need at
    # least `margin_usd`. In fixed_risk mode the size is CAPPED to available
    # margin inside open_trade, so we only need *some* usable margin (enough
    # to clear HL's min notional); otherwise the open would size to ~0.
    # Insufficient → DROP cleanly (no backfill — May 1 decision).
    available = await state.hl.get_available_margin()
    if mode == "fixed_risk":
        # Smallest margin that can still place a min-notional order at this lev.
        required = MIN_NOTIONAL_USD / max(float(event.get("leverage") or state.hl.default_leverage), 1.0)
    else:
        required = margin_usd
    if available is not None and available < required:
        log.warning(
            "DROPPED #%s %s — insufficient margin: $%.2f avail, $%.2f required "
            "(mode=%s, no backfill — signal lost by design)",
            trade_id, coin, available, required, mode,
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
        notifier.notify_skipped(
            coin=coin, side=side, reason="insufficient_margin",
            caller=caller, trade_id=int(trade_id),
            detail=f"${available:.2f} avail < ${required:.2f} needed",
        )
        db.insert_skipped_trade(
            trade_id=int(trade_id), coin=coin, side=side, caller=caller,
            reason="insufficient_margin",
            detail=f"${available:.2f} avail < ${required:.2f} needed",
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
            sizing_mode=mode, margin_usd=margin_usd, risk_usd=risk_usd,
            available_margin=available,
        )
    except LevelSlippageExceeded as exc:
        # Caller's entry is too far from current mid — never chase. Instead,
        # park a GTC bracket AT the caller's level: if price retraces within
        # the TTL we fill exactly where they did; otherwise it expires.
        log.warning(
            "ENTRY SLIPPED #%s %s — mid=%g vs called=%g drift=%.2f%% cap=%.2f%% "
            "→ attempting resting entry",
            trade_id, coin, exc.mid, exc.level,
            exc.drift_pct * 100, exc.slip_pct * 100,
        )
        placed = await _place_resting_entry_for(
            trade_id=int(trade_id), coin=coin, side=side, caller=caller,
            entry=float(entry) if entry else None,
            stop=float(stop) if stop else None,
            leverage=event.get("leverage"),
            mode=mode, margin_usd=margin_usd, risk_usd=risk_usd,
            available=available,
        )
        if placed:
            return
        # Resting not possible (no SL / k-coin / HL rejection) → plain skip.
        _push_activity(
            "blocked",
            f"🎯 SLIPPED {coin} #{trade_id} (mid {_fmt_price(exc.mid)} vs "
            f"called {_fmt_price(exc.level)}, drift {exc.drift_pct*100:.2f}%)",
            int(trade_id),
        )
        _slip_detail = (
            f"mid {_fmt_price(exc.mid)} vs called {_fmt_price(exc.level)} "
            f"({exc.drift_pct*100:.2f}% drift > {exc.slip_pct*100:.2f}% cap); "
            f"resting entry not possible"
        )
        notifier.notify_skipped(
            coin=coin, side=side, reason="entry_slipped", caller=caller,
            trade_id=int(trade_id), detail=_slip_detail,
        )
        db.insert_skipped_trade(
            trade_id=int(trade_id), coin=coin, side=side, caller=caller,
            reason="entry_slipped", detail=_slip_detail,
        )
        state.pending_trades.pop(int(trade_id), None)
        state.fire_refresh()
        return
    except HyperliquidValidationError as exc:
        _safe_notify(f"Open rejected #{trade_id}: {exc}", "negative")
        # Distinguish "asset not on HL" (capture-rate cause) from other
        # rejections so the performance page can show it.
        if "not found on any dex" in str(exc).lower():
            db.insert_skipped_trade(
                trade_id=int(trade_id), coin=coin, side=side, caller=caller,
                reason="ticker_not_found", detail=str(exc),
            )
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


def _bare(name):
    """Strip a HIP-3 dex prefix ("xyz:SILVER" → "SILVER"). HL reports
    positions/orders using the dex-prefixed order_name; the DB and portal
    use the bare coin. Normalize to bare for matching."""
    if not isinstance(name, str):
        return name
    return name.split(":", 1)[1] if ":" in name else name


async def _adopt_untracked_positions(hl_positions: list[dict], only_hl: set) -> None:
    """Re-link live HL positions the DB lost track of back to their portal
    trades, so the bot resumes managing their TPs/stops automatically.

    For each HL position whose coin isn't in the DB, recover the portal
    trade_id from the activity feed (most recent still-open trade for that
    coin), then insert hl_opened_trades + hl_live_trades rows using HL's real
    entry price (= breakeven) and current resting stop. Best-effort: any
    position we can't match to a portal trade is left untouched and logged.
    """
    if state.portal is None or state.hl is None:
        return
    try:
        raw_events = await state.portal.get_activity_feed()
    except Exception:
        log.exception("adopt: could not fetch activity feed — skipping adoption")
        return

    index = build_open_trade_index(raw_events, lambda c: _bare(hl_symbol_for(c)))
    adopted = 0
    for p in hl_positions:
        coin_key = _bare(p.get("coin") or "")
        if coin_key not in only_hl:
            continue
        match = index.get(coin_key)
        if match is None:
            log.warning(
                "adopt: %s open on HL but no matching open portal trade in feed "
                "— cannot recover trade_id, left untouched (manual position?)",
                coin_key,
            )
            continue
        tid = int(match["trade_id"])
        if db.get_live_trade(tid) is not None or db.get_closed_trade(tid) is not None:
            continue  # already known

        try:
            resting_stop = await state.hl.get_resting_stop_price(p.get("coin"))
        except Exception:
            resting_stop = None

        entry = float(p.get("entry_price") or 0.0)
        side = (p.get("side") or match.get("side") or "long")
        try:
            db.insert_opened_trade(
                trade_id=tid, coin=coin_key, side=side,
                entry_price=entry, entry_sl=resting_stop,
                size=float(p.get("size") or 0.0),
                margin=state.hl.margin_usd,
                leverage=p.get("leverage") or state.hl.default_leverage,
                caller=match.get("caller") or "adopted",
                my_fill_price=entry,   # HL's entryPx IS the real avg fill / BE
            )
            db.add_live_trade(tid)
            adopted += 1
            log.warning(
                "adopt: now tracking %s as portal #%s "
                "(entry=%s sl=%s size=%s caller=%s) — bot will manage its TPs/stops",
                coin_key, tid, entry, resting_stop, p.get("size"), match.get("caller"),
            )
            _push_activity(
                "opened",
                f"ADOPTED {coin_key} {side.upper()} #{tid} (entry {_fmt_price(entry)})",
                tid,
            )
        except Exception:
            log.exception("adopt: failed to insert tracking for %s #%s", coin_key, tid)

    if adopted:
        log.warning(
            "adopt: recovered %d untracked position(s) — auto-trail + TP "
            "mirroring now active for them", adopted,
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
            "startup sync: HL has positions not tracked in DB: %s — "
            "attempting adoption so the bot manages them",
            sorted(only_hl),
        )
        await _adopt_untracked_positions(hl_positions, only_hl)
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
        try:
            await _sweep_resting_entries()
        except Exception:
            log.exception("resting-entry sweep failed (will retry next tick)")


async def _sweep_resting_entries() -> None:
    """Graduate filled resting entries; cancel + record expired ones.

    - Filled: the position appeared on HL → reconcile/adoption already
      re-tracked it in hl_live_trades — we just drop the resting row.
    - Expired: TTL passed without a fill → cancel the bracket on HL, drop the
      row, and record the skip so /performance shows why it was missed.
    """
    for row in db.list_resting_entries():
        tid = int(row["trade_id"])
        if db.get_live_trade(tid) is not None:
            db.remove_resting_entry(tid)
            log.info(
                "resting entry #%s %s GRADUATED (filled + adopted)",
                tid, row["coin"],
            )
            continue
        if db.resting_entry_expired(row):
            try:
                if state.hl is not None:
                    await state.hl.cancel_resting_entry(
                        trade_id=tid, portal_coin=row["coin"],
                    )
            except Exception:
                log.exception("expiry cancel failed for resting #%s", tid)
            db.remove_resting_entry(tid)
            detail = (
                f"unfilled after {RESTING_ENTRY_TTL_MIN:g} min at "
                f"{row['entry_px']}"
            )
            log.info("resting entry #%s %s EXPIRED — %s", tid, row["coin"], detail)
            _push_activity(
                "resting",
                f"⌛ RESTING EXPIRED {row['coin']} #{tid}",
                tid,
            )
            notifier.notify_skipped(
                coin=row["coin"], side=row.get("side"),
                reason="resting_expired", caller=row.get("caller"),
                trade_id=tid, detail=detail,
            )
            db.insert_skipped_trade(
                trade_id=tid, coin=row["coin"], side=row.get("side"),
                caller=row.get("caller"), reason="resting_expired",
                detail=detail,
            )
            state.fire_refresh()


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

# ============================================================
# SECTION: Dashboard auth gate
# ============================================================

def _dash_authed() -> bool:
    """True if this browser session may see the dashboard.

    Auth disabled (no DASHBOARD_PASSWORD) → always True, with the public-
    dashboard warning shown instead. Session flag lives in app.storage.user
    (cookie signed with the storage secret)."""
    if not auth_enabled():
        return True
    try:
        return bool(app.storage.user.get("dash_authed", False))
    except Exception:
        return False


@ui.page("/login")
def login_page() -> None:
    if _dash_authed():
        ui.navigate.to("/")
        return

    def _try(_=None) -> None:
        if password_ok(pw.value):
            app.storage.user["dash_authed"] = True
            ui.navigate.to("/")
        else:
            log.warning("dashboard: failed login attempt")
            ui.notify("Wrong password", type="negative")

    with ui.column().classes("absolute-center items-center gap-4"):
        ui.label("🔒 Corgi Copy Trading Bot").classes("text-xl font-bold")
        ui.label("This dashboard controls live trades.").classes(
            "text-sm opacity-60"
        )
        pw = ui.input(
            "Dashboard password", password=True, password_toggle_button=True,
        ).classes("w-72")
        pw.on("keydown.enter", _try)
        ui.button("Log in", on_click=_try).props("color=primary")


@ui.page("/")
def index() -> None:
    if not _dash_authed():
        ui.navigate.to("/login")
        return
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
            ui.button(
                "📊 Performance",
                on_click=lambda: ui.navigate.to("/performance"),
            ).props("flat color=primary")
            if state.dry_run:
                ui.label("DRY RUN").classes("dry-banner")
            if auth_enabled():
                def _logout() -> None:
                    app.storage.user["dash_authed"] = False
                    ui.navigate.to("/login")
                ui.button("Logout", on_click=_logout).props("flat")
            else:
                ui.label("⚠ NO PASSWORD — dashboard is PUBLIC").classes(
                    "dry-banner"
                ).style("background:#742a2a;color:#fff5f5;")

    # ---- sizing controls (live, persisted to bot_settings; read at trade time) ----
    with ui.expansion("⚙ Position Sizing", icon="tune").classes("w-full"):
        _default_margin = state.hl.margin_usd if state.hl else 100.0
        _sizing = db.get_sizing_settings(default_margin_usd=_default_margin)
        with ui.row().classes("items-center gap-4 mt-2"):
            mode_sel = ui.select(
                {"fixed_margin": "Fixed Margin", "fixed_risk": "Fixed Risk (SL-based)"},
                value=_sizing["sizing_mode"], label="Mode",
            ).classes("w-52")
            margin_in = ui.number(
                label="Margin $ / trade", value=_sizing["margin_usd"],
                min=1, step=10,
            ).classes("w-40")
            risk_in = ui.number(
                label="Risk $ / trade", value=_sizing["risk_usd"],
                min=1, step=10,
            ).classes("w-40")

            def _save_sizing() -> None:
                m = mode_sel.value or "fixed_margin"
                db.set_setting("sizing_mode", m)
                try:
                    db.set_setting("margin_usd", str(float(margin_in.value)))
                except (ValueError, TypeError):
                    pass
                try:
                    db.set_setting("risk_usd", str(float(risk_in.value)))
                except (ValueError, TypeError):
                    pass
                log.info(
                    "sizing settings saved via dashboard: mode=%s margin=$%s risk=$%s",
                    m, margin_in.value, risk_in.value,
                )
                summary = (
                    f"Fixed Risk ${risk_in.value}/trade" if m == "fixed_risk"
                    else f"Fixed Margin ${margin_in.value}/trade"
                )
                ui.notify(f"Sizing saved: {summary} (applies to next trade)", type="positive")

            ui.button("Save", on_click=_save_sizing, color="primary")
        ui.label(
            "Fixed Margin: position = margin × leverage. "
            "Fixed Risk: size so a stop-out loses ~Risk $ (±slippage); "
            "falls back to Fixed Margin if a call has no SL, and is capped at "
            "available margin. Changes apply to the next trade — no redeploy."
        ).classes("text-xs opacity-60 mt-1")

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
                    rows = db.get_historic_trades(limit=DASH_HISTORY_ROWS)
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
                    for evt in list(state.activity_feed)[:DASH_FEED_ROWS]:
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


# ============================================================
# SECTION: Performance comparison page (/performance)
# ============================================================

@ui.page("/performance")
async def performance_page() -> None:
    if not _dash_authed():
        ui.navigate.to("/login")
        return
    ui.add_head_html(
        "<style>"
        ".perf-metric { background: #1a202c; color: #e2e8f0; padding: 14px 18px; "
        "  border-radius: 10px; min-width: 180px; }"
        ".perf-metric .label { font-size: 11px; opacity: 0.65; text-transform: uppercase; }"
        ".perf-metric .value { font-size: 24px; font-weight: 700; margin-top: 4px; }"
        ".perf-metric .sub { font-size: 11px; opacity: 0.6; margin-top: 2px; }"
        ".perf-pos { color: #48bb78; }   .perf-neg { color: #fc8181; }"
        ".perf-caveat { background: #44391a; color: #faf0c6; padding: 12px 16px; "
        "  border-radius: 8px; font-size: 12px; line-height: 1.5; }"
        "</style>"
    )

    with ui.header().classes("items-center justify-between"):
        ui.label("📊 Performance vs Portal").classes("text-lg font-bold")
        with ui.row().classes("items-center gap-2"):
            ui.button("← Dashboard", on_click=lambda: ui.navigate.to("/")).props("flat")
            ui.button(
                "⟳ Refresh", on_click=lambda: ui.navigate.reload(),
            ).props("flat color=primary")

    container = ui.column().classes("w-full p-4 gap-4")
    with container:
        ui.label("Loading portal feed + bot history…").classes("text-base opacity-70")

    # ---- gather data ----
    try:
        portal_events: list[dict] = []
        if state.portal is not None:
            try:
                portal_events = await state.portal.get_activity_feed()
            except Exception:
                log.exception("performance: portal feed fetch failed")
                portal_events = []

        bot_opens = db.list_all_opened_trades(limit=10000)
        bot_closeds = db.get_historic_trades(limit=10000)
        bot_live_ids = {int(t["trade_id"]) for t in db.list_live_trades()
                        if t.get("trade_id") is not None}

        allowed_raw = os.environ.get(
            "ALLOWED_CALLERS", "voberoi,pranayyyy,corgil_",
        )
        allowed_callers = {
            c.strip().lower() for c in allowed_raw.split(",") if c.strip()
        }

        sizing = db.get_sizing_settings(
            default_margin_usd=state.hl.margin_usd if state.hl else 100.0,
        )
        per_trade_margin = float(sizing.get("margin_usd") or 100.0)

        # PERSIST the current feed's portal outcomes, then reconcile against
        # ALL persisted history — so matches survive the feed rolling forward
        # (the root cause of the "0 matched" decay). Read-path only; never
        # touches order execution. Persist failures are non-fatal.
        try:
            live_agg = _perf_aggregate_portal(portal_events, allowed_callers)
            for _pt in live_agg.values():
                db.upsert_portal_trade(**_pt)
        except Exception:
            log.exception("performance: portal-trade persist failed (non-fatal)")
        try:
            persisted_portal = {
                int(r["trade_id"]): r for r in db.list_portal_trades()
                if r.get("trade_id") is not None
            }
        except Exception:
            log.exception("performance: load persisted portal trades failed")
            persisted_portal = {}

        # Latest skip reason per trade_id (oldest→newest so newest wins) —
        # lets the analyzer price WHAT each miss cost, split by WHY.
        try:
            skip_map = {
                int(r["trade_id"]): r["reason"]
                for r in reversed(db.list_skipped_trades(limit=5000))
                if r.get("trade_id") is not None
            }
        except Exception:
            log.exception("performance: skip-reason load failed")
            skip_map = {}

        reconciled = _perf_reconcile(
            portal_events=portal_events,
            bot_opens=bot_opens, bot_closeds=bot_closeds,
            bot_live_ids=bot_live_ids,
            allowed_callers=allowed_callers,
            extra_portal_trades=persisted_portal,
            skip_reasons=skip_map,
        )
        summary = _perf_summarize(
            reconciled, per_trade_margin_usd=per_trade_margin,
        )
        curves = _perf_cumulative(
            reconciled, per_trade_margin_usd=per_trade_margin,
        )
    except Exception as exc:
        container.clear()
        with container:
            ui.label("Failed to build report.").classes("text-red-400 font-bold")
            ui.label(str(exc)).classes("text-sm opacity-70")
        log.exception("performance page: build failed")
        return

    container.clear()

    def _signed(usd: Optional[float]) -> str:
        if usd is None:
            return "—"
        sign = "+" if usd >= 0 else "-"
        return f"{sign}${abs(usd):,.2f}"

    def _signed_cls(usd: Optional[float]) -> str:
        if usd is None or usd == 0:
            return ""
        return "perf-pos" if usd > 0 else "perf-neg"

    def _metric(label: str, value_html: str, subtitle: str = "", cls: str = "") -> None:
        with ui.element("div").classes("perf-metric"):
            ui.label(label).classes("label")
            ui.html(f'<div class="value {cls}">{value_html}</div>')
            if subtitle:
                ui.label(subtitle).classes("sub")

    with container:
        # Caveats
        with ui.element("div").classes("perf-caveat w-full"):
            ui.html(
                "<strong>How to read this:</strong> Bot data is from your live SQLite "
                "(<code>hl_closed_trades</code> minus pre-seeded). Portal data is from "
                "the current activity feed window — older closes may have rolled off. "
                "<strong>Portal hypothetical $</strong> = "
                f"<code>${per_trade_margin:.0f} × pnl%</code> per trade (perp ROI on "
                "margin). <strong>Bot actual $</strong> is realized PnL after fees on HL. "
                "Only <em>closed</em> trades count for performance; open positions aren't "
                "included (no unrealized PnL shown)."
            )

        taken = summary["taken"]
        missed = summary["missed"]

        def _usd_or_dash(v, fmt="${:,.2f}"):
            return fmt.format(v) if v is not None else "—"

        # ═══════════ HERO — the numbers that answer "is the bot working" ═══════════
        ui.label("At a glance").classes("text-lg font-bold")
        with ui.row().classes("gap-3 flex-wrap"):
            _metric(
                "Bot realized PnL",
                _signed(summary["bot_total_usd"]),
                f"{taken['count']} closed · win rate {summary['bot_win_rate']*100:.0f}% "
                f"· fees ${summary['bot_fees_usd']:.2f}",
                _signed_cls(summary["bot_total_usd"]),
            )
            _metric(
                "Avg per trade",
                _signed(taken["avg_pnl_usd"]),
                f"best {_signed(taken['best_usd'])} · worst {_signed(taken['worst_usd'])}",
                _signed_cls(taken["avg_pnl_usd"]),
            )
            _metric(
                "Avg margin / trade",
                _usd_or_dash(taken["avg_margin_usd"]),
                f"avg position size {_usd_or_dash(taken['avg_notional_usd'])} notional",
            )
            _metric(
                "Missed trades cost",
                _signed(missed["portal_usd"]),
                f"{missed['count']} missed · {missed['completed']} with known outcome "
                f"(at ${per_trade_margin:.0f}/trade)",
                # positive missed $ = money left on the table = BAD → red
                "perf-neg" if (missed["portal_usd"] or 0) > 0 else "perf-pos",
            )

        # ═══════════ MISSED TRADES — what each skip reason cost ═══════════
        if missed["by_reason"]:
            ui.label("What missed trades cost — by skip reason").classes(
                "text-base font-semibold mt-2"
            )
            ui.label(
                "Portal outcome of every trade the bot did NOT take, priced at "
                f"${per_trade_margin:.0f}/trade. Positive bar = money left on the "
                "table by that skip reason; negative = those skips dodged losers. "
                "This is the chart that says which guard to tune."
            ).classes("text-xs opacity-60")
            _reason_labels = {
                "stale": "🕰️ Stale (bot restart)",
                "blocked_coin_live": "🚫 Coin already live",
                "insufficient_margin": "💸 Insufficient margin",
                "entry_slipped": "🎯 Entry slipped",
                "ticker_not_found": "❓ Not on HL",
                "resting_expired": "⌛ Resting expired",
                "resting_cancelled": "🕒 Resting cancelled",
                "unknown": "⚠️ Unrecorded (pre-tracking)",
            }
            _rows = summary["missed"]["by_reason"]
            try:
                ui.echart({
                    "backgroundColor": "transparent",
                    "tooltip": {"trigger": "axis",
                                "axisPointer": {"type": "shadow"}},
                    "grid": {"left": 220, "right": 80, "top": 10, "bottom": 30},
                    "xAxis": {
                        "type": "value",
                        "axisLabel": {"color": "#a0aec0", "formatter": "${value}"},
                        "splitLine": {"lineStyle": {"color": "#2d3748"}},
                    },
                    "yAxis": {
                        "type": "category",
                        "data": [
                            f"{_reason_labels.get(s['reason'], s['reason'])}  "
                            f"({s['count']}×)"
                            for s in reversed(_rows)
                        ],
                        "axisLabel": {"color": "#e2e8f0", "fontSize": 13},
                    },
                    "series": [{
                        "type": "bar", "barMaxWidth": 30,
                        "label": {"show": True, "position": "right",
                                  "color": "#e2e8f0", "formatter": "${c}"},
                        "data": [
                            {
                                "value": round(s["portal_usd"], 2),
                                "itemStyle": {
                                    "color": "#fc8181" if s["portal_usd"] > 0
                                    else "#48bb78",
                                    "borderRadius": 4,
                                },
                            }
                            for s in reversed(_rows)
                        ],
                    }],
                }).classes("w-full").style(
                    f"height: {max(160, 60 * len(_rows) + 60)}px;"
                )
            except Exception:
                log.debug("ui.echart unavailable for missed-by-reason", exc_info=True)
                for s in _rows:
                    ui.label(
                        f"{_reason_labels.get(s['reason'], s['reason'])}: "
                        f"{s['count']}× → {_signed(s['portal_usd'])}"
                    ).classes("text-sm")
            if missed["biggest_missed"]:
                bm = missed["biggest_missed"]
                ui.label(
                    f"😬 Biggest single miss: {bm['coin']} #{bm['trade_id']} "
                    f"({bm['caller'] or '?'}) — would have made "
                    f"{_signed(bm['portal_usd'])} · skipped because: "
                    f"{_reason_labels.get(bm['reason'], bm['reason'])}"
                ).classes("text-sm perf-caveat w-full")

        # Headline: direct apples-to-apples comparison
        ui.label("Direct comparison — trades present on both sides").classes("text-base font-semibold mt-2")
        if summary["direct_count"] == 0:
            ui.label(
                "No trades currently match on both sides. Your bot's closed "
                "trades and the portal's recent closes don't overlap yet — the "
                "portal feed is a rolling window, but portal outcomes are now "
                "persisted, so matches will populate as new trades close on both "
                "sides. The 'Wider totals' below are the accurate per-side numbers."
            ).classes("text-sm opacity-70 perf-caveat w-full")
        with ui.row().classes("gap-3 flex-wrap"):
            _metric(
                "Portal hypothetical",
                _signed(summary["direct_portal_usd"]),
                f"{summary['direct_count']} matched trades",
                _signed_cls(summary["direct_portal_usd"]),
            )
            _metric(
                "Bot actual",
                _signed(summary["direct_bot_usd"]),
                "after fees",
                _signed_cls(summary["direct_bot_usd"]),
            )
            _metric(
                "Difference (bot − portal)",
                _signed(summary["direct_diff_usd"]),
                "negative = bot underperformed",
                _signed_cls(summary["direct_diff_usd"]),
            )

        # Cumulative PnL curves — the headline "are we tracking the callers" visual
        if curves["portal"] or curves["bot"]:
            ui.label("Cumulative PnL over time").classes("text-base font-semibold mt-2")
            ui.label(
                "Each curve sums its own closed trades in time order. Portal uses "
                f"${per_trade_margin:.0f} × pnl% at the portal close time; bot uses "
                "realized PnL at the bot close time. Flat stretches = no closes."
            ).classes("text-xs opacity-60")
            try:
                ui.echart({
                    "backgroundColor": "transparent",
                    "tooltip": {
                        "trigger": "axis",
                        "axisPointer": {"type": "cross"},
                    },
                    "legend": {
                        "data": ["Portal hypothetical $", "Bot actual $",
                                 "Missed trades (cost) $"],
                        "textStyle": {"color": "#e2e8f0"},
                        "top": 4,
                    },
                    "grid": {"left": 70, "right": 30, "top": 44, "bottom": 48},
                    "xAxis": {
                        "type": "time",
                        "axisLabel": {"color": "#a0aec0"},
                        "axisLine": {"lineStyle": {"color": "#4a5568"}},
                        "splitLine": {"show": False},
                    },
                    "yAxis": {
                        "type": "value",
                        "axisLabel": {"color": "#a0aec0", "formatter": "${value}"},
                        "splitLine": {"lineStyle": {"color": "#2d3748"}},
                    },
                    "series": [
                        {
                            "name": "Portal hypothetical $", "type": "line",
                            "step": "end", "showSymbol": True, "symbolSize": 6,
                            "lineStyle": {"width": 2.5, "color": "#63b3ed"},
                            "itemStyle": {"color": "#63b3ed"},
                            "areaStyle": {"opacity": 0.08, "color": "#63b3ed"},
                            "data": curves["portal"],
                        },
                        {
                            "name": "Bot actual $", "type": "line",
                            "step": "end", "showSymbol": True, "symbolSize": 6,
                            "lineStyle": {"width": 2.5, "color": "#f6ad55"},
                            "itemStyle": {"color": "#f6ad55"},
                            "areaStyle": {"opacity": 0.08, "color": "#f6ad55"},
                            "data": curves["bot"],
                        },
                        {
                            "name": "Missed trades (cost) $", "type": "line",
                            "step": "end", "showSymbol": True, "symbolSize": 5,
                            "lineStyle": {"width": 2, "color": "#fc8181",
                                          "type": "dashed"},
                            "itemStyle": {"color": "#fc8181"},
                            "data": curves.get("missed", []),
                        },
                    ],
                }).classes("w-full").style("height: 360px;")
            except Exception:
                log.debug("ui.echart unavailable for cumulative curve", exc_info=True)
                ui.label(
                    "Chart unavailable in this NiceGUI version — totals above still apply."
                ).classes("text-xs opacity-60")

        # Wider totals
        ui.label("Wider totals — full window each side").classes("text-base font-semibold mt-2")
        with ui.row().classes("gap-3 flex-wrap"):
            _metric(
                f"Portal — {summary['portal_completed_count']} completed",
                _signed(summary["portal_total_usd"]),
                f"win rate {summary['portal_win_rate']*100:.0f}%",
                _signed_cls(summary["portal_total_usd"]),
            )
            _metric(
                f"Bot — {summary['bot_closed_count']} closed",
                _signed(summary["bot_total_usd"]),
                f"fees ${summary['bot_fees_usd']:.2f} · win rate {summary['bot_win_rate']*100:.0f}%",
                _signed_cls(summary["bot_total_usd"]),
            )

        # Status breakdown
        ui.label("Trade status breakdown").classes("text-base font-semibold mt-2")
        with ui.row().classes("gap-3 flex-wrap"):
            labels = {
                STATUS_COPIED_CLOSED: "Copied + closed",
                STATUS_COPIED_OPEN: "Copied (still open)",
                STATUS_COPIED_ORPHAN: "Copied (orphan)",
                STATUS_MISSED: "Missed (portal had it, bot didn't)",
                STATUS_BOT_ONLY: "Bot-only (portal feed rolled off)",
            }
            for status, label in labels.items():
                count = summary["status_counts"].get(status, 0)
                if count == 0:
                    continue
                with ui.element("div").classes("perf-metric"):
                    ui.label(label).classes("label")
                    ui.html(f'<div class="value">{count}</div>')

        # Skip-reason breakdown (persisted — survives log rotation)
        try:
            skip_counts = db.get_skip_reason_counts()
        except Exception:
            skip_counts = {}
        if skip_counts:
            ui.label("Why signals were skipped (persisted)").classes("text-base font-semibold mt-2")
            ui.label(
                "Recorded at skip time, so this survives log rotation. "
                "ticker_not_found = caller's symbol isn't on HL (or needs an alias)."
            ).classes("text-xs opacity-60")
            _skip_labels = {
                "stale": "🕰️ Stale (pre-startup)",
                "blocked_coin_live": "🚫 Blocked (coin already live)",
                "insufficient_margin": "💸 Insufficient margin",
                "entry_slipped": "🎯 Entry slipped (>cap)",
                "ticker_not_found": "❓ Ticker not on HL",
            }
            _skip_colors = {
                "stale": "#ed8936",
                "blocked_coin_live": "#ecc94b",
                "insufficient_margin": "#fc8181",
                "entry_slipped": "#9f7aea",
                "ticker_not_found": "#63b3ed",
            }
            _sorted_skips = sorted(skip_counts.items(), key=lambda x: -x[1])
            with ui.row().classes("w-full items-center gap-4 flex-wrap"):
                try:
                    ui.echart({
                        "backgroundColor": "transparent",
                        "tooltip": {"trigger": "item",
                                    "formatter": "{b}: {c} ({d}%)"},
                        "series": [{
                            "type": "pie",
                            "radius": ["45%", "75%"],
                            "avoidLabelOverlap": True,
                            "itemStyle": {"borderRadius": 6,
                                          "borderColor": "#1a202c",
                                          "borderWidth": 2},
                            "label": {"color": "#e2e8f0",
                                      "formatter": "{b}\n{c}"},
                            "data": [
                                {
                                    "name": _skip_labels.get(r, r),
                                    "value": n,
                                    "itemStyle": {
                                        "color": _skip_colors.get(r, "#a0aec0"),
                                    },
                                }
                                for r, n in _sorted_skips
                            ],
                        }],
                    }).classes("flex-grow").style("height: 280px; min-width: 380px;")
                except Exception:
                    log.debug("ui.echart unavailable for skip donut", exc_info=True)
                with ui.column().classes("gap-2"):
                    for reason, count in _sorted_skips:
                        with ui.element("div").classes("perf-metric"):
                            ui.label(_skip_labels.get(reason, reason)).classes("label")
                            ui.html(f'<div class="value">{count}</div>')

        # Per-caller chart
        ui.label("Performance by caller").classes("text-base font-semibold mt-2")
        callers = summary["by_caller"]
        if callers:
            try:
                ui.echart({
                    "backgroundColor": "transparent",
                    "tooltip": {"trigger": "axis",
                                "axisPointer": {"type": "shadow"}},
                    "legend": {"data": ["Portal hypothetical $", "Bot actual $"],
                               "textStyle": {"color": "#e2e8f0"}, "top": 4},
                    "grid": {"left": 70, "right": 30, "top": 44, "bottom": 36},
                    "xAxis": {
                        "type": "category",
                        "data": [c["caller"] for c in callers],
                        "axisLabel": {"color": "#e2e8f0", "fontSize": 13},
                        "axisLine": {"lineStyle": {"color": "#4a5568"}},
                    },
                    "yAxis": {
                        "type": "value",
                        "axisLabel": {"color": "#a0aec0", "formatter": "${value}"},
                        "splitLine": {"lineStyle": {"color": "#2d3748"}},
                    },
                    "series": [
                        {"name": "Portal hypothetical $", "type": "bar",
                         "barMaxWidth": 56,
                         "itemStyle": {"color": "#63b3ed",
                                       "borderRadius": [6, 6, 0, 0]},
                         "label": {"show": True, "position": "top",
                                   "color": "#e2e8f0",
                                   "formatter": "${c}"},
                         "data": [round(c["portal_pnl_usd"], 2) for c in callers]},
                        {"name": "Bot actual $", "type": "bar",
                         "barMaxWidth": 56,
                         "itemStyle": {"color": "#f6ad55",
                                       "borderRadius": [6, 6, 0, 0]},
                         "label": {"show": True, "position": "top",
                                   "color": "#e2e8f0",
                                   "formatter": "${c}"},
                         "data": [round(c["bot_pnl_usd"], 2) for c in callers]},
                    ],
                }).classes("w-full").style("height: 340px;")
            except Exception:
                # ui.echart may not be available in older NiceGUI — fall back to
                # a simple table so the page still renders.
                log.debug("ui.echart unavailable, using table fallback", exc_info=True)
                _caller_rows = [{
                    "caller": c["caller"],
                    "portal_pnl": _signed(c["portal_pnl_usd"]),
                    "bot_pnl": _signed(c["bot_pnl_usd"]),
                    "completed": c["portal_completed"],
                    "missed": c["bot_missed"],
                } for c in callers]
                ui.table(
                    columns=[
                        {"name": "caller", "label": "Caller", "field": "caller"},
                        {"name": "portal_pnl", "label": "Portal $", "field": "portal_pnl"},
                        {"name": "bot_pnl", "label": "Bot $", "field": "bot_pnl"},
                        {"name": "completed", "label": "Portal completed", "field": "completed"},
                        {"name": "missed", "label": "Bot missed", "field": "missed"},
                    ],
                    rows=_caller_rows, row_key="caller",
                ).classes("w-full")

        # Per-trade table — capped so the WebSocket state-sync stays small.
        ui.label("Trade-level reconciliation").classes("text-base font-semibold mt-2")
        ui.label(
            f"Newest first, most recent {PERF_TABLE_ROWS}. Negative bot $ = realized loss."
        ).classes("text-xs opacity-60")
        trade_rows = []
        for r in reconciled[:PERF_TABLE_ROWS]:
            trade_rows.append({
                "trade_id": r.trade_id,
                "coin": r.coin,
                "side": (r.side or "").upper(),
                "caller": r.caller or "—",
                "status": r.bot_status,
                "portal_pnl_pct": (
                    f"{r.portal_pnl_pct*100:+.2f}%" if r.portal_pnl_pct is not None else "—"
                ),
                "portal_pnl_usd": (
                    _signed(r.portal_pnl_pct * per_trade_margin)
                    if r.portal_pnl_pct is not None else "—"
                ),
                "bot_pnl_usd": _signed(r.bot_pnl_usd) if r.bot_pnl_usd is not None else "—",
                "bot_margin": (
                    f"${r.bot_margin_usd:,.0f}" if r.bot_margin_usd else "—"
                ),
                "bot_notional": (
                    f"${r.bot_notional_usd:,.0f}" if r.bot_notional_usd else "—"
                ),
                "bot_fee_usd": (
                    f"${r.bot_fee_usd:.2f}" if r.bot_fee_usd is not None else "—"
                ),
                "skip_reason": r.skip_reason or (
                    "—" if r.bot_status != STATUS_MISSED else "unrecorded"
                ),
            })
        ui.table(
            columns=[
                {"name": "trade_id", "label": "ID", "field": "trade_id", "sortable": True},
                {"name": "coin", "label": "Coin", "field": "coin", "sortable": True},
                {"name": "side", "label": "Side", "field": "side"},
                {"name": "caller", "label": "Caller", "field": "caller", "sortable": True},
                {"name": "status", "label": "Status", "field": "status", "sortable": True},
                {"name": "skip_reason", "label": "Skip reason", "field": "skip_reason", "sortable": True},
                {"name": "portal_pnl_pct", "label": "Portal %", "field": "portal_pnl_pct"},
                {"name": "portal_pnl_usd", "label": "Portal $", "field": "portal_pnl_usd"},
                {"name": "bot_pnl_usd", "label": "Bot $", "field": "bot_pnl_usd"},
                {"name": "bot_margin", "label": "Margin", "field": "bot_margin"},
                {"name": "bot_notional", "label": "Size $", "field": "bot_notional"},
                {"name": "bot_fee_usd", "label": "Fee", "field": "bot_fee_usd"},
            ],
            rows=trade_rows, row_key="trade_id",
            pagination={"rowsPerPage": 50},
        ).classes("w-full")


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
        "resting":        "text-purple-300",
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
    import secrets as _secrets
    if not auth_enabled():
        log.warning(
            "⚠ DASHBOARD_PASSWORD is not set — the dashboard (Auto Mode, "
            "Cancel, sizing) is PUBLICLY accessible. Set DASHBOARD_PASSWORD "
            "in the environment to require login."
        )
    ui.run(
        title="Corgi Copy Trading Bot",
        port=int(os.environ.get("PORT", 8080)),
        host=os.environ.get("HOST", "0.0.0.0"),
        reload=False,
        show=False,
        dark=True,
        # ROOT CAUSE of the "Message too long / too large for WebSocket
        # transmission" freeze (seen even on the near-empty login page):
        # NiceGUI buffers the last `message_history_length` outgoing messages
        # per client to replay on reconnect. This bot pushes UI updates every
        # 1–3s (activity feed + tick timers), so at the default 1000 the replay
        # sent on (re)connect blows past the WebSocket size limit and the
        # client can NEVER establish a working connection → every page freezes.
        # A short buffer only needs to cover a brief network blip.
        message_history_length=int(os.environ.get("DASH_MESSAGE_HISTORY", 50)),
        # Signs the app.storage.user session cookie. Without the env var a
        # random per-boot secret is used → sessions reset on every redeploy
        # (harmless: you just log in again). Set it to persist sessions.
        storage_secret=(
            os.environ.get("DASHBOARD_STORAGE_SECRET")
            or _secrets.token_hex(32)
        ),
    )
