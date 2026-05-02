#!/usr/bin/env python3
"""
Debug what events the portal is returning and how they're being parsed.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.portal import PortalClient


async def main():
    print("=" * 80)
    print("EVENT PARSING DEBUG")
    print("=" * 80)
    print()

    portal = PortalClient()
    await portal.start()

    try:
        # Fetch new events (this will parse and dedupe)
        events = await portal.fetch_new_events()

        print(f"Portal returned {len(events)} NEW events (after dedup):")
        print()

        for evt in events:
            etype = evt.get("type")
            trade_id = evt.get("trade_id")
            coin = evt.get("coin")
            caller = evt.get("caller")

            print(f"  Event: {etype:<15} Trade #{trade_id:<5} {caller:<12} {coin:<10}")

            # Show key fields for each type
            if etype == "new_trade":
                print(f"    entry_price={evt.get('entry_price')} sl={evt.get('stop_loss')} side={evt.get('side')}")
            elif etype == "stop_update":
                print(f"    new_stop={evt.get('new_stop')} old_stop={evt.get('old_stop')}")
            elif etype == "tp_hit":
                print(f"    size_pct={evt.get('size_pct')}% tp_price={evt.get('tp_price')}")
            elif etype == "full_close":
                print(f"    exit_price={evt.get('exit_price')} stop_triggered={evt.get('stop_triggered')}")

        print()
        print("=" * 80)

        if len(events) == 0:
            print()
            print("⚠️  Zero events returned!")
            print("This means:")
            print("  - Portal has no new events, OR")
            print("  - All events were already seen (deduped), OR")
            print("  - All events were filtered (non-whitelisted callers, bets)")
            print()

            # Try fetching raw activity feed to see what's there
            print("Fetching RAW activity feed (no parsing/dedup)...")
            raw = await portal.get_activity_feed()
            print(f"Raw activity feed has {len(raw)} events")

            # Show first 10
            print()
            print("First 10 raw events:")
            for i, r in enumerate(raw[:10]):
                etype = r.get("type") or r.get("eventType")
                tid = r.get("tradeId")
                caller = r.get("caller") or r.get("userTag")
                coin = r.get("coin")
                print(f"  {i+1}. type={etype:<20} trade_id={tid:<5} caller={caller:<12} coin={coin}")

    finally:
        await portal.close()


if __name__ == "__main__":
    asyncio.run(main())
