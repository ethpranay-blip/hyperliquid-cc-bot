"""
Pure trailing-stop computation for the Corgi Calls copy-trading bot.

No I/O, no nicegui, no HL SDK — just the math, so it's fully unit-testable.
`main._auto_trail_stop_after_tp` wires this up with DB lookups + the live HL
mid validity guard.

Corgi Calls trailing rules (confirmed canonical, May 2026):
    TP1 hit → stop to breakeven (the ACTUAL fill price)
    TP2 hit → stop stays at breakeven
    TP3 hit → stop to TP1 price
    TP4 hit → stop to TP2 price

Ratchet invariant: a stop is NEVER loosened. When the trailing target and the
current stop disagree, keep whichever is tighter (closer to price on the
protective side):
    long  → tighter = HIGHER stop → max(target, current)
    short → tighter = LOWER  stop → min(target, current)

This is why the bot keeps a user's profit-locking stop instead of dragging it
back to breakeven: e.g. a short already stopped at $15.75 stays there when TP1
would only move it to BE $16, because $15.75 is tighter for a short.
"""
from __future__ import annotations

from typing import Optional


def compute_trailed_stop(
    *,
    is_long: bool,
    tp_num: int,
    fill_price: float,
    current_stop: Optional[float],
    tp1_price: Optional[float] = None,
    tp2_price: Optional[float] = None,
) -> tuple[Optional[float], str]:
    """Return (new_stop, reason) for an auto-trail after `tp_num` books.

    `fill_price` is the bot's REAL average fill (breakeven), NOT the
    slippage-padded limit. `current_stop` is the trade's current effective
    SL (latest sl_update or entry_sl); None if unknown.

    Returns (None, reason) when there's no valid move (e.g. TP3 hit but no
    recorded TP1 price, or an unhandled tp_num).
    """
    # 1) Resolve the rule target for this TP level.
    if tp_num in (1, 2):
        target = fill_price
        reason = f"TP{tp_num} hit → BE"
    elif tp_num == 3:
        if tp1_price is None:
            return None, "TP3 hit but no TP1 price recorded — SL not moved"
        target = tp1_price
        reason = "TP3 hit → TP1"
    elif tp_num == 4:
        if tp2_price is None:
            return None, "TP4 hit but no TP2 price recorded — SL not moved"
        target = tp2_price
        reason = "TP4 hit → TP2"
    else:
        return None, f"TP{tp_num} — no trailing rule defined"

    # 2) Ratchet: never loosen. Keep the tighter of {target, current_stop}.
    if current_stop is not None:
        if is_long:
            ratcheted = max(target, current_stop)
        else:
            ratcheted = min(target, current_stop)
        if ratcheted != target:
            reason += (
                f" (kept tighter existing stop {ratcheted:g} "
                f"over target {target:g})"
            )
        target = ratcheted

    return target, reason


def member_breakeven_floor(
    *,
    caller_stop: float,
    fill_price: Optional[float],
    is_long: bool,
    tp_hit: bool,
) -> float:
    """Once we've booked a TP, never let a MIRRORED caller stop sit worse than
    the MEMBER's own breakeven (our real fill).

    Guards the "caller's stop wins" policy against a caller who edits their entry
    DOWN and then moves their "breakeven" stop to that new, lower entry: a member
    who's in at the original price would otherwise get a stop below their own fill
    = a locked loss where the caller is flat. So after the first TP, we protect
    OUR breakeven, not the caller's edited entry.

    Before any TP (tp_hit False) or when the fill is unknown, the caller's stop is
    their genuine risk stop and is mirrored as-is.
    """
    if not tp_hit or fill_price is None:
        return caller_stop
    floor = float(fill_price)
    return max(caller_stop, floor) if is_long else min(caller_stop, floor)
