"""
Strict price-level slippage cap for entries, TPs, and full closes.

Trading rule: every action — open, partial TP, full close — happens at (or
better than) the level the caller posted, plus a small slippage cushion
(default 0.5%). If price has moved past that cap by the time the bot fires
the order, the order's own IOC limit prevents it from filling and the bot
logs a level_slipped skip.

Pure helpers (no I/O); the HL client wires them into open_trade /
_close_common, and main raises LevelSlippageExceeded → notify_skipped.

Direction convention (matches HL):
  is_buy=True  (long entry / short close) — worst fill = HIGHER price.
                  Cap:  limit = level × (1 + slip_pct)   (fills if ASK ≤ limit)
  is_buy=False (short entry / long close) — worst fill = LOWER price.
                  Cap:  limit = level × (1 - slip_pct)   (fills if BID ≥ limit)
"""
from __future__ import annotations

import os
from typing import Optional

# Default cap; per-deploy override via env, per-trade override via setting.
DEFAULT_MAX_LEVEL_SLIPPAGE_PCT = 0.005  # 0.5%


def get_default_slip_pct() -> float:
    """Read MAX_LEVEL_SLIPPAGE_PCT from env, falling back to 0.5%.

    Accepts either a decimal (0.005) or a percentage-looking value (0.5 →
    treated as 0.5%, i.e. 0.005). Values > 0.5 are clamped at 0.5 (50%) as a
    sanity guard against a typo making the cap a no-op.
    """
    raw = os.environ.get("MAX_LEVEL_SLIPPAGE_PCT", "").strip()
    if not raw:
        return DEFAULT_MAX_LEVEL_SLIPPAGE_PCT
    try:
        v = float(raw)
    except (ValueError, TypeError):
        return DEFAULT_MAX_LEVEL_SLIPPAGE_PCT
    # Heuristic: >= 1 likely means "5" meaning "5%" → 0.05.
    if v >= 1:
        v = v / 100.0
    if v <= 0:
        return DEFAULT_MAX_LEVEL_SLIPPAGE_PCT
    return min(v, 0.5)


class LevelSlippageExceeded(Exception):
    """Current price is too far from the caller's level to execute within cap."""

    def __init__(
        self,
        *,
        action: str,        # "entry" | "tp" | "close"
        level: float,
        mid: float,
        is_buy: bool,
        slip_pct: float,
    ) -> None:
        self.action = action
        self.level = level
        self.mid = mid
        self.is_buy = is_buy
        self.slip_pct = slip_pct
        # drift = how far adverse the mid has moved relative to level
        if is_buy:
            adverse = max(0.0, mid - level)
        else:
            adverse = max(0.0, level - mid)
        self.drift_pct = (adverse / level) if level > 0 else 0.0
        super().__init__(
            f"{action} level-slippage exceeded: mid={mid:g} vs level={level:g} "
            f"is_buy={is_buy} drift={self.drift_pct*100:.2f}% cap={slip_pct*100:.2f}%"
        )


def slippage_capped_limit(
    *, level: float, is_buy: bool, slip_pct: float,
) -> float:
    """Return the IOC limit that caps fills to within slip_pct of `level`.

    For a buy, fills happen at ASK ≤ limit, so limit = level × (1 + slip).
    For a sell, fills happen at BID ≥ limit, so limit = level × (1 - slip).
    Caller must apply round_px to match HL's price precision before sending.
    """
    if level is None or level <= 0:
        raise ValueError("slippage_capped_limit: level must be > 0")
    if slip_pct < 0:
        raise ValueError("slippage_capped_limit: slip_pct must be >= 0")
    if is_buy:
        return level * (1 + slip_pct)
    return level * (1 - slip_pct)


def check_within_slip(
    *, mid: float, level: float, is_buy: bool, slip_pct: float,
) -> tuple[bool, float]:
    """Return (executable_within_cap, adverse_drift_pct).

    Pre-flight gate before submitting an IOC. If False, the IOC would
    not fill at the capped limit; better to skip cleanly with a notification.
    """
    if level is None or level <= 0 or mid is None or mid <= 0:
        return (False, 0.0)
    if is_buy:
        # Buying: cap is upper. mid must be ≤ level × (1 + slip).
        cap = level * (1 + slip_pct)
        ok = mid <= cap
        drift = max(0.0, (mid - level) / level)
    else:
        # Selling: cap is lower. mid must be ≥ level × (1 - slip).
        cap = level * (1 - slip_pct)
        ok = mid >= cap
        drift = max(0.0, (level - mid) / level)
    return (ok, drift)


def enforce_slip(
    *,
    action: str,
    mid: float,
    level: Optional[float],
    is_buy: bool,
    slip_pct: float,
) -> None:
    """Raise LevelSlippageExceeded if the current mid is outside the cap.

    No-op when `level` is None / non-positive — older trades or events
    without a caller-posted reference fall back to the legacy mid-based
    cushion in the caller's code.
    """
    if level is None or level <= 0:
        return
    ok, _drift = check_within_slip(
        mid=mid, level=level, is_buy=is_buy, slip_pct=slip_pct,
    )
    if not ok:
        raise LevelSlippageExceeded(
            action=action, level=float(level), mid=float(mid),
            is_buy=is_buy, slip_pct=slip_pct,
        )
