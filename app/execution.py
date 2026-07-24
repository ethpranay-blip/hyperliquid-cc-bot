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

# Per-action default caps. Two-tier policy:
#   ENTRY + SL  → 0.5%  — precision matters most (avoid bad fills / late closes)
#   TP / profit → 1.0%  — booking a winner at 1% off is still a winner
# Per-deploy override via env vars; future dashboard exposure planned.
DEFAULT_ENTRY_SLIP_PCT = 0.005   # 0.5%
DEFAULT_TP_SLIP_PCT    = 0.010   # 1.0%  (reactive partial-TP fallback path)
DEFAULT_SL_SLIP_PCT    = 0.005   # 0.5%  (HL's resting SL trigger is the safety net)
DEFAULT_TP_BAND_PCT    = 0.005   # 0.5%  (protective band on PRE-PLACED TP trigger-limits)
DEFAULT_TP_SIZE_PCT    = 25.0    # book 25% per TP when the caller doesn't give a size


def _read_env_slip(env_var: str, default: float) -> float:
    """Parse a slip-pct env var. Accepts a decimal (0.005) or a
    percentage-looking value (0.5 → 0.005). Garbage / non-positive →
    default. Clamped at 0.5 (50%) so a typo can't make the cap a no-op.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except (ValueError, TypeError):
        return default
    if v >= 1:
        v = v / 100.0
    if v <= 0:
        return default
    return min(v, 0.5)


def get_entry_slip_pct() -> float:
    """Cap for new-trade entries (long buy / short sell). Default 0.5%."""
    return _read_env_slip("MAX_ENTRY_SLIPPAGE_PCT", DEFAULT_ENTRY_SLIP_PCT)


def get_tp_slip_pct() -> float:
    """Cap for partial-TP fills and manual profit-booking closes. Default 1.0%."""
    return _read_env_slip("MAX_TP_SLIPPAGE_PCT", DEFAULT_TP_SLIP_PCT)


def get_sl_slip_pct() -> float:
    """Cap for SL-triggered full closes. Default 0.5%. HL's own resting SL
    trigger is the safety net if the bot's market-close misses the cap."""
    return _read_env_slip("MAX_SL_SLIPPAGE_PCT", DEFAULT_SL_SLIP_PCT)


def get_default_slip_pct() -> float:
    """Back-compat for callers that don't yet specify per-action cap.
    Returns the ENTRY cap (the conservative choice)."""
    return get_entry_slip_pct()


def get_tp_band_pct() -> float:
    """Protective-limit band for PRE-PLACED TP trigger orders. Default 0.5% —
    the caller wants TPs filled within 0.5% of their posted TP price."""
    return _read_env_slip("TP_PREPLACE_BAND_PCT", DEFAULT_TP_BAND_PCT)


def preplace_tps_enabled() -> bool:
    """Kill-switch for TP pre-placement (default ON). Set PREPLACE_TPS=0/false
    to fall back to purely reactive TP handling without a code change."""
    raw = os.environ.get("PREPLACE_TPS", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _tp_num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def normalize_take_profits(tps, *, default_pct: float = DEFAULT_TP_SIZE_PCT) -> list[dict]:
    """Normalize a portal take-profits value into [{'price','size_pct'}, ...].

    Accepts a scalar, a list of numbers, or a list of dicts (price under any of
    price/tp/level/value/tpPrice; size under size/sizePct/pct/percent). A size
    given as a fraction in (0,1] is read as a percentage (0.25 → 25%). Missing
    size → default_pct (25%). Non-positive prices are dropped. Cumulative
    size_pct is capped at 100 (reduce-only would reject any oversell anyway).
    Order is preserved (TP1, TP2, …).
    """
    out: list[dict] = []
    if tps is None:
        return out
    if not isinstance(tps, (list, tuple)):
        tps = [tps]
    total = 0.0
    for t in tps:
        price = None
        pct = None
        if isinstance(t, dict):
            for k in ("price", "tp", "level", "value", "tpPrice"):
                price = _tp_num(t.get(k))
                if price is not None:
                    break
            for k in ("size_pct", "sizePct", "pct", "percent", "size"):
                pct = _tp_num(t.get(k))
                if pct is not None:
                    break
        else:
            price = _tp_num(t)
        if price is None or price <= 0:
            continue
        if pct is None or pct <= 0:
            pct = default_pct
        elif pct <= 1.0:          # a fraction like 0.25 → 25%
            pct = pct * 100.0
        remaining = max(0.0, 100.0 - total)
        pct = min(pct, remaining)
        if pct <= 0:
            break                 # already at 100% booked
        total += pct
        out.append({"price": float(price), "size_pct": float(pct)})
    return out


def plan_tp_legs(
    *,
    position_size: float,
    tps,
    ref_price: float,
    is_long: bool,
    band_pct: Optional[float] = None,
    default_pct: float = DEFAULT_TP_SIZE_PCT,
) -> list[dict]:
    """Plan reduce-only TP legs for a filled position (pure; caller rounds).

    Returns [{'trigger_px','limit_px','size','size_pct'}, ...] where:
      - trigger_px = the caller's TP price (order fires when price reaches it)
      - limit_px   = protective limit `band_pct` inside the TP (fills within band)
      - size       = position_size × size_pct/100 (UNROUNDED — caller applies sz_decimals)

    Only TPs on the profit side survive (long: above ref; short: below ref) so
    a mis-posted TP can't fire instantly as a loss. Sizes/prices are unrounded;
    the caller rounds and drops any leg that rounds to zero size.
    """
    if band_pct is None:
        band_pct = get_tp_band_pct()
    if position_size is None or position_size <= 0:
        return []
    legs: list[dict] = []
    for tp in normalize_take_profits(tps, default_pct=default_pct):
        price = tp["price"]
        if is_long and price <= ref_price:
            continue
        if (not is_long) and price >= ref_price:
            continue
        # exit is the opposite side of the position
        limit_px = slippage_capped_limit(
            level=price, is_buy=(not is_long), slip_pct=band_pct,
        )
        legs.append({
            "trigger_px": price,
            "limit_px": limit_px,
            "size": position_size * (tp["size_pct"] / 100.0),
            "size_pct": tp["size_pct"],
        })
    return legs


def sl_triggers_immediately(
    *, new_stop: Optional[float], mid: Optional[float], is_long: bool,
) -> bool:
    """True if placing a stop at `new_stop` would fire instantly vs `mid`.

    A long's SL is a sell that triggers when price falls TO/below it — so a
    stop set at or ABOVE the current mid triggers immediately. A short's SL
    is a buy that triggers when price rises TO/above it — so a stop at or
    BELOW the current mid triggers immediately. Either case = an unintended
    instant market close.

    Returns False when inputs are unknown (can't judge → let other guards
    decide; don't block a legitimate move on missing data).
    """
    if new_stop is None or mid is None or mid <= 0:
        return False
    if is_long:
        return new_stop >= mid
    return new_stop <= mid


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
