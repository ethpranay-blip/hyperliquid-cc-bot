"""
Pure position-sizing for the copy-trading bot. No I/O, no SDK — testable.

Two modes (chosen live from the dashboard, persisted in bot_settings):

  fixed_margin (default / current behavior):
      size = (margin_usd * leverage) / mid
      Risk varies with how far the SL sits from entry.

  fixed_risk:
      size = risk_usd / |mid - sl_px|
      So a stop-out loses ~risk_usd (±slippage), independent of leverage.
      Leverage only affects how much margin is locked, not the dollar risk.
      - Capped so the required margin never exceeds available_margin.
      - Falls back to fixed_margin when there's no usable SL (sl_px None or
        zero entry-to-SL distance).
"""
from __future__ import annotations

from typing import Optional

FIXED_MARGIN = "fixed_margin"
FIXED_RISK = "fixed_risk"


def compute_position_size(
    *,
    mode: str,
    mid: float,
    leverage: float,
    sz_decimals: int,
    sl_px: Optional[float],
    margin_usd: float,
    risk_usd: float,
    available_margin: Optional[float],
    min_notional: float = 10.0,
) -> tuple[float, str]:
    """Return (size, basis_description).

    `mid` is the entry approximation (live mid). `available_margin` may be
    None (unknown → no cap applied). Raises ValueError if the resulting
    notional is below `min_notional` or inputs are invalid.
    """
    if mid is None or mid <= 0:
        raise ValueError("invalid mid for sizing")
    if leverage <= 0:
        raise ValueError("invalid leverage for sizing")

    use_risk = (
        mode == FIXED_RISK
        and sl_px is not None
        and sl_px > 0
        and abs(mid - sl_px) > 0
    )

    if use_risk:
        distance = abs(mid - sl_px)
        raw_size = risk_usd / distance
        margin_needed = (raw_size * mid) / leverage
        basis = (
            f"fixed_risk risk=${risk_usd:g} dist={distance:g} "
            f"margin≈${margin_needed:.2f}"
        )
        # Cap so required margin never exceeds what's available.
        if available_margin is not None and 0 < available_margin < margin_needed:
            capped_notional = available_margin * leverage
            raw_size = capped_notional / mid
            basis += f" (capped to avail ${available_margin:.2f})"
        size = round(raw_size, int(sz_decimals))
    else:
        if mode == FIXED_RISK:
            basis = (
                f"fixed_risk→fixed_margin fallback (no usable SL) "
                f"margin=${margin_usd:g}"
            )
        else:
            basis = f"fixed_margin margin=${margin_usd:g}"
        raw_size = (margin_usd * leverage) / mid
        size = round(raw_size, int(sz_decimals))

    notional = size * mid
    if notional < min_notional:
        raise ValueError(
            f"computed notional ${notional:.2f} below min ${min_notional:.2f} "
            f"({basis})"
        )
    return size, basis
