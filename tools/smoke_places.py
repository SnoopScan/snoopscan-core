"""Live smoke of the Places source (engine/places) against Google Maps.

One results page, the detail panel for each place, and leadgen enrichment of
each website — exactly what POST /v1/places/search does, run in-process with
every deployed rung. Nothing is persisted. Costs browser time and, on a blocked
exit, proxy bandwidth.

    ENGINE_STEALTH_HEADLESS=false .venv/bin/python tools/smoke_places.py
    .venv/bin/python tools/smoke_places.py "plumbers" "Manchester"

Lives in the repo, not /tmp: a reboot empties /tmp and the absence of a checker
throws no error.
"""

from __future__ import annotations

import asyncio
import sys

from engine.api.deps import get_fetchers
from engine.core.scrape_service import ScrapeService
from engine.places.models import PlacesSearchRequest
from engine.places.service import PlacesService


async def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "coffee shops"
    location = sys.argv[2] if len(sys.argv) > 2 else "Austin TX"
    fetchers = get_fetchers()
    print("tiers wired:", ", ".join(str(t) for t in fetchers))
    scrape = ScrapeService(fetchers, persist=False)
    places = PlacesService(scrape, persist=False)

    req = PlacesSearchRequest(
        query=query, location=location, limit=3, includeDetails=True, enrich=True
    )
    data = await places.search(req)
    print(
        f"\nplaces={len(data.places)} search_pages={data.search_pages} "
        f"detail_fetches={data.detail_fetches} enriched={data.enriched}"
    )
    for p in data.places:
        print(f"\n• {p.name}  {p.rating}★ {p.review_count or ''}  {p.category}")
        print(f"  {p.address}")
        print(f"  {p.website}  {p.phone}  ({p.latitude}, {p.longitude})")
        if p.contacts:
            c = p.contacts
            print(
                f"  contacts: status={c.status} emails={c.emails[:3]} "
                f"form={c.contact_form_url} social={sorted(c.social_links)[:3]}"
            )

    for fetcher in fetchers.values():
        close = getattr(fetcher, "aclose", None)
        if close:
            await close()
    return 0 if data.places else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
