#!/usr/bin/env python3
"""Check current pending trades state"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Can't easily access state.pending_trades from here, so check the DB instead
from app import db

def main():
    print("=" * 80)
    print("CURRENT BOT STATE")
    print("=" * 80)
    print()

    # Live trades in DB
    live = db.list_live_trades()
    print(f"✅ hl_live_trades: {len(live)} trades")
    for t in live:
        print(f"  #{t['trade_id']}: {t.get('coin', 'N/A'):<12} {t.get('side', 'N/A'):<6} {t.get('caller', 'N/A')}")
    print()

    # Pending trades in DB
    pending = db.list_pending_trades()
    print(f"✅ hl_pending_trades: {len(pending)} trades")
    for t in pending:
        print(f"  #{t['trade_id']}: {t.get('coin', 'N/A'):<12} {t.get('side', 'N/A'):<6} reason={t.get('reason', 'N/A')}")
    print()

    # Recent closed trades
    closed = db.get_historic_trades(limit=10)
    print(f"✅ Recent hl_closed_trades: {len(closed)} (showing last 10)")
    for t in closed:
        print(f"  #{t['trade_id']}: {t.get('coin', 'N/A'):<12} {t.get('side', 'N/A'):<6} pnl={t.get('pnl', 0):.2f}")
    print()

    print("=" * 80)

if __name__ == "__main__":
    main()
