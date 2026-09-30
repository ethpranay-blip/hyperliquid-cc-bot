"""
Read-only stop audit — are our HL stops in sync with the callers' CURRENT stops?

For every live trade in the DB it compares:
  • the caller's current stop  (portal /me/trades → trade.stop, PORTAL units)
  • our resting stop on HL      (reduce-only trigger, TP legs excluded, HL units)
scaling the caller's stop into HL space (scale_stop_for_k) and flagging any
drift beyond DRIFT_PCT. Also flags positions with NO resting stop.

READ-ONLY: it never places, moves, or cancels an order. Safe to run any time.
Run on the box that has the live env (Railway) — it needs portal auth + the HL
wallet address:

    python stop_audit.py

Env: CORGI_DB_PATH, PORTAL_USER/PORTAL_PASSWORD (or stored cookies),
     HL_WALLET_ADDRESS.
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import db                                         # noqa: E402
from app.portal import PortalClient                        # noqa: E402
from app.hyperliquid_client import (                       # noqa: E402
    hl_symbol_for, portal_base_coin, scale_stop_for_k, _is_tp_cloid_raw,
)

DRIFT_PCT = float(os.environ.get("STOP_AUDIT_DRIFT_PCT", "0.5")) / 100.0
HL_URL = "https://api.hyperliquid.xyz/info"
DEXES = ("", "xyz", "cash", "flx")
ADDR = os.environ.get("HL_WALLET_ADDRESS", "")


def _hl_open_orders() -> list[dict]:
    """All resting orders across the dexes the bot uses (public read)."""
    out: list[dict] = []
    with httpx.Client(timeout=15) as c:
        for dex in DEXES:
            body = {"type": "frontendOpenOrders", "user": ADDR}
            if dex:
                body["dex"] = dex
            try:
                oo = c.post(HL_URL, json=body).json()
            except Exception as e:
                print(f"  (HL openOrders dex={dex!r} failed: {e})")
                continue
            for o in oo or []:
                if isinstance(o, dict):
                    out.append(o)
    return out


def _our_sl_px(order_name: str, orders: list[dict]):
    """The triggerPx of OUR resting stop for order_name — a reduce-only trigger
    that is NOT one of our pre-placed TP legs (by cloid). None if absent."""
    for o in orders:
        if o.get("coin") != order_name or not o.get("reduceOnly"):
            continue
        otype = str(o.get("orderType") or "").lower()
        if not (o.get("isTrigger") or "trigger" in otype or "stop" in otype):
            continue
        if _is_tp_cloid_raw(o.get("cloid")):
            continue  # a TP leg, not the stop
        if str(o.get("tpsl") or "").lower().startswith("tp"):
            continue
        try:
            return float(o.get("triggerPx"))
        except (TypeError, ValueError):
            return None
    return None


async def main() -> None:
    if not ADDR:
        print("HL_WALLET_ADDRESS not set — cannot audit."); return
    portal = PortalClient()
    await portal.start()

    # Caller stops: one GET of all followed trades → {trade_id: stop(portal)}
    caller_stop: dict[int, float] = {}
    try:
        for w in (await portal.get_trades()) or []:
            t = w.get("trade") if isinstance(w, dict) else None
            tid = (w or {}).get("tradeId") or (t or {}).get("tradeId")
            s = (t or {}).get("stop") if isinstance(t, dict) else None
            if tid is not None and s not in (None, "", 0):
                try:
                    caller_stop[int(tid)] = float(s)
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        print(f"portal get_trades failed: {e}")

    orders = _hl_open_orders()
    live = db.list_live_trades()

    print(f"\n=== STOP AUDIT — {len(live)} live trades (drift tol {DRIFT_PCT*100:.2f}%) ===")
    hdr = f"{'#id':>6} {'COIN':10} {'CALLER(portal)':>16} {'OURS(HL)':>14} {'CALLER→HL':>14}  VERDICT"
    print(hdr)
    ok = drift = nostop = nocaller = 0
    for t in live:
        tid = int(t["trade_id"])
        coin = portal_base_coin(t.get("coin") or "")
        order_name = hl_symbol_for(coin)
        ours = _our_sl_px(order_name, orders)
        cs = caller_stop.get(tid)
        cs_hl = scale_stop_for_k(coin, cs) if cs is not None else None

        if ours is None:
            verdict, tag = "⚠️  NO STOP ON HL", "nostop"; nostop += 1
        elif cs is None:
            verdict, tag = "· caller stop unknown (closed?)", "nocaller"; nocaller += 1
        else:
            d = abs(ours - cs_hl) / cs_hl if cs_hl else 1.0
            if d <= DRIFT_PCT:
                verdict, tag = "OK", "ok"; ok += 1
            else:
                verdict, tag = f"⚠️  DRIFT {d*100:.1f}%  (set HL stop → {cs_hl:g})", "drift"; drift += 1
        print(f"{tid:>6} {coin:10} {str(cs):>16} {str(ours):>14} {str(cs_hl):>14}  {verdict}")

    print(f"\nSummary: {ok} in-sync · {drift} drifted · {nostop} missing stop · "
          f"{nocaller} caller-unknown")
    if drift or nostop:
        print("ACTION: fix the ⚠️ rows on HL (or re-check why the update didn't apply).")


if __name__ == "__main__":
    asyncio.run(main())
