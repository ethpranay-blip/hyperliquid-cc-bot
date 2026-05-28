"""
Pure helpers for adopting live HL positions the DB has lost track of.

When the bot restarts onto a fresh/empty DB (or missed events during a
freeze), it may find positions open on Hyperliquid that aren't in
hl_live_trades. To resume managing their TPs/stops it needs to recover the
*portal* trade_id for each — that's the key everything else (tp_hit,
stop_update, full_close) is routed by.

`build_open_trade_index` reconstructs {normalized_coin -> open trade info}
from the portal activity feed: the most-recent `trade_opened` per coin that
hasn't subsequently closed. No I/O, no nicegui — unit-testable.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

_OPEN_KINDS = {"trade_opened", "trade_open"}
_CLOSE_KINDS = {
    "trade_closed", "trade_close", "full_close", "close",
    "position_closed", "stop_triggered", "sl_triggered",
    "auto_close", "stale_close", "cancelled", "canceled",
}


def _event_kind(raw: dict) -> str:
    return str(raw.get("type") or raw.get("eventType") or raw.get("action") or "").lower()


def _trade_id(raw: dict) -> Optional[int]:
    tid = raw.get("tradeId") or raw.get("trade_id")
    try:
        return int(tid) if tid is not None else None
    except (ValueError, TypeError):
        return None


def build_open_trade_index(
    raw_events: list[dict],
    normalize_coin: Callable[[str], str],
) -> dict[str, dict[str, Any]]:
    """Return {normalized_coin: {"trade_id", "caller", "side"}} for the most
    recent still-open trade per coin.

    `normalize_coin` maps a portal coin (e.g. "PEPE") to the same key space the
    caller uses for HL positions (e.g. "kPEPE"); pass
    `lambda c: _bare(hl_symbol_for(c))` from main.

    A trade is "still open" if it has a trade_opened event and NO later
    close-style event for the same trade_id. Events are sorted newest-first so
    the first open seen per coin wins.
    """
    events = [e for e in raw_events if isinstance(e, dict)]
    # Newest-first (defensive — don't assume feed order).
    events.sort(key=lambda e: e.get("timestamp") or 0, reverse=True)

    closed_ids: set[int] = set()
    for raw in events:
        if _event_kind(raw) in _CLOSE_KINDS:
            tid = _trade_id(raw)
            if tid is not None:
                closed_ids.add(tid)

    index: dict[str, dict[str, Any]] = {}
    for raw in events:
        if _event_kind(raw) not in _OPEN_KINDS:
            continue
        tid = _trade_id(raw)
        coin = raw.get("coin")
        if tid is None or not coin:
            continue
        if tid in closed_ids:
            continue
        key = normalize_coin(coin)
        if key in index:
            continue  # newest already recorded
        index[key] = {
            "trade_id": tid,
            "caller": raw.get("caller"),
            "side": (raw.get("side") or "long").lower(),
        }
    return index
