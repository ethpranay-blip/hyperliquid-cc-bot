#!/usr/bin/env python3
"""
Check the deduplication state - see what event IDs are marked as "seen"
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.portal import PortalClient


async def main():
    print("=" * 80)
    print("DEDUPLICATION STATE CHECK")
    print("=" * 80)
    print()

    portal = PortalClient()
    await portal.start()

    try:
        # Check how many event IDs are currently marked as seen
        seen_count = len(portal._seen_event_ids)
        print(f"Portal has {seen_count} event IDs marked as 'seen'")
        print()

        if seen_count > 0:
            print(f"Sample of seen event IDs (first 20):")
            for i, eid in enumerate(sorted(list(portal._seen_event_ids))[:20]):
                print(f"  {eid}")
            print()

        # Now fetch new events - this will mark more as seen
        print("Fetching new events (will dedup against 'seen' set)...")
        events = await portal.fetch_new_events()

        print(f"After fetch: {len(portal._seen_event_ids)} event IDs marked as seen")
        print(f"Returned {len(events)} NEW events")
        print()

        # Now try again - should return 0 since they're all "seen" now
        print("Fetching again immediately (should return 0 due to dedup)...")
        events2 = await portal.fetch_new_events()
        print(f"Second fetch returned {len(events2)} events (expected 0)")
        print()

        # The problem: if the bot has been running for 20 hours, all historical
        # events are already in the _seen set, so new polls return []
        print("=" * 80)
        print("DIAGNOSIS:")
        print()
        print("The portal client maintains a deduplication set of event IDs.")
        print("Once an event is seen ONCE, it's never returned again.")
        print()
        print("If the bot has been running since before these trades were opened,")
        print("the events were returned on the FIRST poll after they appeared,")
        print("but if the event router didn't process them (silent failure),")
        print("they're now permanently marked as 'seen' and won't come back.")
        print()
        print("This is why manually calling fetch_new_events() works (returns events),")
        print("but the running bot sees nothing (events already marked seen).")

    finally:
        await portal.close()


if __name__ == "__main__":
    asyncio.run(main())
