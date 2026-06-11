"""
Bot-vs-portal performance reconciliation. Pure logic — no I/O, no nicegui —
so it can be unit-tested without the SDK or DB connection.

Inputs:
  portal_events  — raw activity-feed list from PortalClient.get_activity_feed()
  bot_opens      — list[dict] of hl_opened_trades rows (any with trade_id)
  bot_closeds    — list[dict] of hl_closed_trades rows (pre-seeded excluded
                   by the caller via the close_type filter)
  bot_live_ids   — set[int] of currently-live trade_ids
  allowed_callers — set[str] of caller usernames (lowercase) to include

Outputs:
  reconcile() → list[ReconciledTrade] — one row per known trade, joining
  what the portal recorded with what the bot did.

  summarize() → dict of headline metrics + per-caller breakdown.

Caveats baked into the math:
- Portal activity feed has a rolling window; trades that closed weeks ago
  may have rolled off. Trades in the bot DB but not in the portal window
  get status="bot_only" and contribute to "bot $" but not the comparison.
- Portal's pnlPct is treated as ROI on margin (the perp display
  convention). Hypothetical $ = per_trade_margin * pnl_pct.
- "Open" portal trades (no close event yet) carry no pnl_pct and don't
  affect the comparison totals, but show in the reconciliation list.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


PORTAL_OPEN_KINDS = {"trade_opened", "trade_open"}
PORTAL_CLOSE_KINDS = {
    "trade_closed", "trade_close", "full_close", "close",
    "position_closed", "stop_triggered", "sl_triggered",
    "auto_close", "stale_close", "cancelled", "canceled",
}

# bot_status values
STATUS_COPIED_CLOSED = "copied_closed"   # bot opened + closed; full comparison
STATUS_COPIED_OPEN = "copied_open"       # bot opened, still open on HL
STATUS_COPIED_ORPHAN = "copied_orphan"   # bot opened, no close + not live
STATUS_MISSED = "missed"                 # portal had it, bot didn't act
STATUS_BOT_ONLY = "bot_only"             # bot has it, portal feed window doesn't


@dataclass
class ReconciledTrade:
    trade_id: int
    coin: str
    caller: Optional[str]
    side: str  # "long" / "short"
    portal_opened_at: Optional[int] = None    # epoch ms
    portal_closed_at: Optional[int] = None
    portal_pnl_pct: Optional[float] = None    # decimal (0.15 = +15%)
    portal_close_price: Optional[float] = None
    bot_status: str = STATUS_MISSED
    bot_pnl_usd: Optional[float] = None
    bot_fee_usd: Optional[float] = None
    bot_size: Optional[float] = None
    bot_entry_price: Optional[float] = None
    bot_exit_price: Optional[float] = None
    bot_closed_at: Optional[int] = None      # epoch ms, parsed from row "at"


def _int_or_none(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _float_or_none(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _iso_to_ms(v: Any) -> Optional[int]:
    """Parse a DB ISO-8601 UTC timestamp ("2026-06-01T12:34:56+00:00") to
    epoch ms. Returns None on anything unparseable."""
    if not v or not isinstance(v, str):
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def reconcile(
    portal_events: list[dict],
    bot_opens: list[dict],
    bot_closeds: list[dict],
    bot_live_ids: set[int],
    allowed_callers: set[str],
) -> list[ReconciledTrade]:
    """Build the per-trade reconciliation list.

    Joins by portal trade_id. Callers not in `allowed_callers` are filtered
    (case-insensitive). Bot trades whose trade_id isn't in the portal window
    are appended with status=bot_only so the user still sees them.
    """
    allowed_lc = {c.lower() for c in allowed_callers}

    # --- Step 1: aggregate portal events by trade_id ---
    portal_trades: dict[int, dict] = {}
    for evt in portal_events:
        if not isinstance(evt, dict):
            continue
        kind = str(evt.get("type") or evt.get("eventType") or "").lower()
        tid = _int_or_none(evt.get("tradeId") or evt.get("trade_id"))
        if tid is None:
            continue
        caller = (evt.get("caller") or "").strip()
        if caller and caller.lower() not in allowed_lc:
            continue
        slot = portal_trades.setdefault(tid, {
            "trade_id": tid, "coin": None, "caller": None,
            "side": "long", "opened_at": None, "closed_at": None,
            "pnl_pct": None, "close_price": None,
        })
        if evt.get("coin") and not slot["coin"]:
            slot["coin"] = evt.get("coin")
        if caller and not slot["caller"]:
            slot["caller"] = caller
        if evt.get("side") and slot["side"] == "long":
            slot["side"] = str(evt.get("side")).lower()
        ts = _int_or_none(evt.get("timestamp"))
        if kind in PORTAL_OPEN_KINDS:
            if slot["opened_at"] is None or (ts is not None and ts < slot["opened_at"]):
                slot["opened_at"] = ts
        elif kind in PORTAL_CLOSE_KINDS:
            if slot["closed_at"] is None or (ts is not None and ts > slot["closed_at"]):
                slot["closed_at"] = ts
            pp = _float_or_none(evt.get("pnlPct"))
            if pp is not None:
                slot["pnl_pct"] = pp
            cp = _float_or_none(evt.get("closePrice") or evt.get("exitPrice"))
            if cp is not None:
                slot["close_price"] = cp

    # --- Step 2: index bot data by trade_id ---
    bot_open_by_id = {int(r["trade_id"]): r for r in bot_opens if r.get("trade_id") is not None}
    bot_closed_by_id = {
        int(r["trade_id"]): r for r in bot_closeds
        if r.get("trade_id") is not None
        and r.get("close_type") != "pre-seeded"
    }
    bot_live_set = {int(t) for t in bot_live_ids}

    # --- Step 3: build reconciled list from portal trades first ---
    reconciled: list[ReconciledTrade] = []
    portal_ids_seen: set[int] = set()
    for tid, p in portal_trades.items():
        # Drop trades from non-allowed callers (caller may be missing on the
        # open event but present on the close — we only kept allowed above).
        if p["caller"] and p["caller"].lower() not in allowed_lc:
            continue
        portal_ids_seen.add(tid)
        rt = ReconciledTrade(
            trade_id=tid,
            coin=p["coin"] or "?",
            caller=p["caller"],
            side=p["side"],
            portal_opened_at=p["opened_at"],
            portal_closed_at=p["closed_at"],
            portal_pnl_pct=p["pnl_pct"],
            portal_close_price=p["close_price"],
        )
        if tid in bot_closed_by_id:
            bc = bot_closed_by_id[tid]
            rt.bot_status = STATUS_COPIED_CLOSED
            rt.bot_pnl_usd = _float_or_none(bc.get("pnl"))
            rt.bot_fee_usd = _float_or_none(bc.get("fee"))
            rt.bot_size = _float_or_none(bc.get("size"))
            rt.bot_entry_price = _float_or_none(bc.get("entry_price"))
            rt.bot_exit_price = _float_or_none(bc.get("exit_price"))
            rt.bot_closed_at = _iso_to_ms(bc.get("closed_at") or bc.get("at"))
        elif tid in bot_live_set:
            rt.bot_status = STATUS_COPIED_OPEN
            if tid in bot_open_by_id:
                rt.bot_size = _float_or_none(bot_open_by_id[tid].get("size"))
                rt.bot_entry_price = _float_or_none(bot_open_by_id[tid].get("entry_price"))
        elif tid in bot_open_by_id:
            rt.bot_status = STATUS_COPIED_ORPHAN
            rt.bot_size = _float_or_none(bot_open_by_id[tid].get("size"))
            rt.bot_entry_price = _float_or_none(bot_open_by_id[tid].get("entry_price"))
        else:
            rt.bot_status = STATUS_MISSED
        reconciled.append(rt)

    # --- Step 4: append bot-only trades (portal feed has rolled off) ---
    for tid, bc in bot_closed_by_id.items():
        if tid in portal_ids_seen:
            continue
        caller = (bc.get("caller") or "").strip() or None
        if caller and caller.lower() not in allowed_lc:
            continue
        reconciled.append(ReconciledTrade(
            trade_id=tid,
            coin=bc.get("coin") or "?",
            caller=caller,
            side=(bc.get("side") or "long").lower(),
            bot_status=STATUS_BOT_ONLY,
            bot_pnl_usd=_float_or_none(bc.get("pnl")),
            bot_fee_usd=_float_or_none(bc.get("fee")),
            bot_size=_float_or_none(bc.get("size")),
            bot_entry_price=_float_or_none(bc.get("entry_price")),
            bot_exit_price=_float_or_none(bc.get("exit_price")),
            bot_closed_at=_iso_to_ms(bc.get("closed_at") or bc.get("at")),
        ))

    # --- Step 5: sort newest-first by opened-at (portal) else by bot info ---
    reconciled.sort(
        key=lambda r: (r.portal_opened_at or r.portal_closed_at or 0, r.trade_id),
        reverse=True,
    )
    return reconciled


def summarize(
    reconciled: list[ReconciledTrade],
    *,
    per_trade_margin_usd: float,
) -> dict:
    """Headline metrics + per-caller breakdown.

    Direct-comparison subset: trades where BOTH a portal pnl_pct AND a bot
    pnl_usd exist. This is the apples-to-apples view ("on the same trades,
    here's what each side made").
    Portal hypothetical (all): every completed portal trade, including ones
    the bot didn't take — the "what you'd make manually mirroring vob".
    Bot actual (all): every bot-closed trade, including ones the portal
    feed has rolled off.
    """
    direct = [
        r for r in reconciled
        if r.portal_pnl_pct is not None and r.bot_pnl_usd is not None
    ]
    direct_portal_usd = sum(r.portal_pnl_pct * per_trade_margin_usd for r in direct)
    direct_bot_usd = sum(r.bot_pnl_usd for r in direct)

    portal_completed = [r for r in reconciled if r.portal_pnl_pct is not None]
    portal_all_usd = sum(r.portal_pnl_pct * per_trade_margin_usd for r in portal_completed)

    bot_closed = [
        r for r in reconciled
        if r.bot_status in (STATUS_COPIED_CLOSED, STATUS_BOT_ONLY)
        and r.bot_pnl_usd is not None
    ]
    bot_all_usd = sum(r.bot_pnl_usd for r in bot_closed)
    bot_fees_usd = sum(r.bot_fee_usd or 0.0 for r in bot_closed)

    # Win-rate
    portal_wins = sum(1 for r in portal_completed if (r.portal_pnl_pct or 0) > 0)
    bot_wins = sum(1 for r in bot_closed if (r.bot_pnl_usd or 0) > 0)

    # Status counts
    status_counts: dict[str, int] = {}
    for r in reconciled:
        status_counts[r.bot_status] = status_counts.get(r.bot_status, 0) + 1

    # Per-caller breakdown (uses all portal completed trades + matching bot rows)
    by_caller: dict[str, dict] = {}
    for r in reconciled:
        c = (r.caller or "unknown").lower()
        s = by_caller.setdefault(c, {
            "caller": c,
            "portal_completed": 0,
            "portal_wins": 0,
            "portal_pnl_usd": 0.0,
            "bot_copied_closed": 0,
            "bot_missed": 0,
            "bot_pnl_usd": 0.0,
        })
        if r.portal_pnl_pct is not None:
            s["portal_completed"] += 1
            if r.portal_pnl_pct > 0:
                s["portal_wins"] += 1
            s["portal_pnl_usd"] += r.portal_pnl_pct * per_trade_margin_usd
        if r.bot_status == STATUS_COPIED_CLOSED:
            s["bot_copied_closed"] += 1
            if r.bot_pnl_usd is not None:
                s["bot_pnl_usd"] += r.bot_pnl_usd
        elif r.bot_status == STATUS_MISSED:
            s["bot_missed"] += 1
        elif r.bot_status == STATUS_BOT_ONLY and r.bot_pnl_usd is not None:
            s["bot_pnl_usd"] += r.bot_pnl_usd

    by_caller_list = sorted(by_caller.values(), key=lambda x: -x["portal_completed"])

    return {
        # Direct comparison (apples-to-apples)
        "direct_count": len(direct),
        "direct_portal_usd": direct_portal_usd,
        "direct_bot_usd": direct_bot_usd,
        "direct_diff_usd": direct_bot_usd - direct_portal_usd,

        # Wider totals
        "portal_completed_count": len(portal_completed),
        "portal_total_usd": portal_all_usd,
        "portal_win_rate": (portal_wins / len(portal_completed)) if portal_completed else 0.0,
        "bot_closed_count": len(bot_closed),
        "bot_total_usd": bot_all_usd,
        "bot_fees_usd": bot_fees_usd,
        "bot_net_usd": bot_all_usd - bot_fees_usd if bot_all_usd is not None else None,
        "bot_win_rate": (bot_wins / len(bot_closed)) if bot_closed else 0.0,

        # Composition
        "status_counts": status_counts,
        "by_caller": by_caller_list,
        "per_trade_margin_usd": per_trade_margin_usd,
    }


def cumulative_series(
    reconciled: list[ReconciledTrade],
    *,
    per_trade_margin_usd: float,
) -> dict:
    """Time-ordered cumulative-PnL curves for charting.

    Returns {"portal": [[ts_ms, cum_usd], ...], "bot": [[ts_ms, cum_usd], ...]}.

    - Portal curve: every completed portal trade (pnl_pct present), at its
      portal close timestamp, $ = pnl_pct × per_trade_margin.
    - Bot curve: every bot-closed trade (pnl present), at its bot close
      timestamp.
    Points missing a usable timestamp are dropped (a curve needs an x-axis);
    each curve is independently sorted oldest→newest and cumulatively summed.
    """
    portal_pts: list[tuple[int, float]] = []
    bot_pts: list[tuple[int, float]] = []
    for r in reconciled:
        if r.portal_pnl_pct is not None and r.portal_closed_at:
            portal_pts.append(
                (int(r.portal_closed_at), r.portal_pnl_pct * per_trade_margin_usd)
            )
        if r.bot_pnl_usd is not None and r.bot_closed_at:
            bot_pts.append((int(r.bot_closed_at), float(r.bot_pnl_usd)))

    def _accumulate(pts: list[tuple[int, float]]) -> list[list[float]]:
        pts.sort(key=lambda p: p[0])
        out, total = [], 0.0
        for ts, delta in pts:
            total += delta
            out.append([ts, round(total, 2)])
        return out

    return {"portal": _accumulate(portal_pts), "bot": _accumulate(bot_pts)}
