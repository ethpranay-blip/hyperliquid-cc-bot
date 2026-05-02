#!/usr/bin/env python3
"""
Quick diagnostic: compare portal feed vs DB vs HL mainnet for the three whitelisted callers.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add app to path
sys.path.insert(0, str(Path(__file__).parent))

from app import db
from app.portal import PortalClient
from app.hyperliquid_client import HyperliquidClient


async def main():
    print("=" * 80)
    print("DIAGNOSTIC: Portal vs DB vs HL Mainnet")
    print("=" * 80)
    print()

    # 1. Check portal activity feed for open trades from whitelisted callers
    print("1️⃣  Checking portal activity feed...")
    print("-" * 80)

    portal = PortalClient()
    await portal.start()

    try:
        # Fetch activity feed
        events = await portal.get_activity_feed()

        # Filter for open trades from whitelisted callers
        whitelisted = {"voberoi", "pranayyyy", "corgil_"}
        portal_open_trades = {}

        for event in events:
            event_type = event.get("type") or event.get("eventType") or ""
            caller = (
                event.get("userTag")
                or event.get("caller")
                or (event.get("trade") or {}).get("userTag")
            )
            trade_id = event.get("tradeId") or (event.get("trade") or {}).get("id")
            status = event.get("status") or (event.get("trade") or {}).get("status")

            if caller in whitelisted and trade_id:
                trade = event.get("trade") or {}
                coin = event.get("coin") or trade.get("coin") or trade.get("symbol")
                side = event.get("side") or trade.get("side")

                # Track if status is "open" or if it's a trade_opened event
                if status == "open" or event_type in {"trade_opened", "new_trade"}:
                    portal_open_trades[trade_id] = {
                        "trade_id": trade_id,
                        "caller": caller,
                        "coin": coin,
                        "side": side,
                        "status": status,
                        "event_type": event_type,
                    }

        print(f"Portal shows {len(portal_open_trades)} open trades from whitelisted callers:")
        for tid, info in sorted(portal_open_trades.items()):
            print(f"  #{tid}: {info['caller']:<12} {info['coin']:<10} {info['side']:<6} ({info['status']})")
        print()

    finally:
        await portal.close()

    # 2. Check database hl_live_trades
    print("2️⃣  Checking database (hl_live_trades)...")
    print("-" * 80)

    db_live = db.list_live_trades()
    db_trade_ids = {t["trade_id"] for t in db_live}

    print(f"Database shows {len(db_live)} live trades:")
    for t in db_live:
        print(f"  #{t['trade_id']}: {t.get('caller', 'N/A'):<12} {t.get('coin', 'N/A'):<10} {t.get('side', 'N/A'):<6}")
    print()

    # 3. Check HL mainnet
    print("3️⃣  Checking HL mainnet positions...")
    print("-" * 80)

    hl = HyperliquidClient()
    await hl.start_price_feed()

    try:
        positions = await hl.open_positions()

        if positions is None:
            print("⚠️  Failed to fetch HL positions (API error)")
        elif len(positions) == 0:
            print("No open positions on HL mainnet")
        else:
            print(f"HL mainnet shows {len(positions)} open positions:")
            for p in positions:
                print(f"  {p['coin']:<15} {p['side']:<6} size={p['size']:<10.4f} entry={p['entry_price']}")
        print()

    finally:
        await hl.stop_price_feed()

    # 4. Gap analysis
    print("4️⃣  Gap Analysis")
    print("-" * 80)

    portal_ids = set(portal_open_trades.keys())

    # Trades on portal but not in DB
    missing_in_db = portal_ids - db_trade_ids
    if missing_in_db:
        print(f"⚠️  {len(missing_in_db)} trade(s) open on portal but NOT in DB:")
        for tid in sorted(missing_in_db):
            info = portal_open_trades[tid]
            print(f"  #{tid}: {info['caller']} - {info['coin']} {info['side']}")
    else:
        print("✅ All portal trades are in DB")

    print()

    # Trades in DB but not on portal
    extra_in_db = db_trade_ids - portal_ids
    if extra_in_db:
        print(f"⚠️  {len(extra_in_db)} trade(s) in DB but NOT on portal (may be closed):")
        for tid in sorted(extra_in_db):
            t = next((x for x in db_live if x["trade_id"] == tid), None)
            if t:
                print(f"  #{tid}: {t.get('caller', 'N/A')} - {t.get('coin', 'N/A')} {t.get('side', 'N/A')}")
    else:
        print("✅ No extra trades in DB")

    print()

    # Compare DB vs HL positions
    if positions is not None:
        hl_coins = {p["coin"] for p in positions}
        db_coins = {t["coin"] for t in db_live if t.get("coin")}

        print("Coin-level comparison (DB vs HL mainnet):")
        missing_on_hl = db_coins - hl_coins
        if missing_on_hl:
            print(f"  ⚠️  DB has positions for {missing_on_hl} but HL doesn't")

        extra_on_hl = hl_coins - db_coins
        if extra_on_hl:
            print(f"  ⚠️  HL has positions for {extra_on_hl} but DB doesn't")

        if not missing_on_hl and not extra_on_hl:
            print("  ✅ DB and HL coin sets match")

    print()

    # 5. Check recent logs for new_trade processing
    print("5️⃣  Checking logs for last new_trade event...")
    print("-" * 80)

    log_path = Path(__file__).parent / "app.log"
    if log_path.exists():
        with open(log_path, "r") as f:
            lines = f.readlines()

        # Search backwards for "new_trade" events
        last_new_trade = None
        for line in reversed(lines[-5000:]):  # Check last 5000 lines
            if "new_trade" in line.lower() and ("handle_" in line or "processing" in line or "event" in line):
                last_new_trade = line.strip()
                break

        if last_new_trade:
            print(f"Last new_trade event in logs:")
            print(f"  {last_new_trade}")
        else:
            print("⚠️  No recent new_trade events found in logs (checked last 5000 lines)")
    else:
        print(f"⚠️  Log file not found at {log_path}")

    print()
    print("=" * 80)
    print("Diagnostic complete")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
