#!/usr/bin/env python3
"""
Clean up stale trades #667 and #668 from hl_live_trades.
These trades don't exist on the portal anymore and HL has no positions.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app import db

def main():
    print("Cleaning up stale trades from hl_live_trades...")
    print()

    # Check what's in there first
    live = db.list_live_trades()
    print(f"Before cleanup: {len(live)} live trades in DB")
    for t in live:
        print(f"  #{t['trade_id']}: {t.get('coin', 'N/A'):<10} {t.get('side', 'N/A'):<6} {t.get('caller', 'N/A')}")
    print()

    # Remove #667 and #668
    for trade_id in [667, 668]:
        if db.get_live_trade(trade_id):
            print(f"Removing trade #{trade_id} from hl_live_trades...")
            db.remove_live_trade(trade_id)
        else:
            print(f"Trade #{trade_id} not in hl_live_trades (already gone)")

    print()
    # Check after
    live = db.list_live_trades()
    print(f"After cleanup: {len(live)} live trades in DB")
    for t in live:
        print(f"  #{t['trade_id']}: {t.get('coin', 'N/A'):<10} {t.get('side', 'N/A'):<6} {t.get('caller', 'N/A')}")
    print()
    print("✅ Cleanup complete")

if __name__ == "__main__":
    main()
