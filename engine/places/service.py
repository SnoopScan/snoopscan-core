"""Places search: the rendered Maps list, optionally each place's detail panel,
optionally leadgen's contacts for each website (12-places-source.md).

Every fetch goes through the ordinary ScrapeService, so Places inherits the
ladder, the proxy layer, consent handling and the cache without owning any of
it. The results page is a JavaScript application — tier 0 returns a shell and
the soft-block climb would reach the browser anyway — so the browser tier is
asked for directly rather than paid for twice.

Cost is reported in units the route bills on: one `places_search` per results
page, one `places_detail` per detail panel. Enrichment bills through the fetch
keys leadgen already uses; it is the existing pipeline doing its existing job.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from urllib.parse import quote_plus

import structlog

from engine.core.errors import EngineError
from engine.core.models import ScrapeOptions
from engine.places.models import Place, PlaceContacts, PlacesSearchData, PlacesSearchRequest
from engine.places.parse import parse_detail, parse_results

logger = structlog.get_logger(__name__)

MAPS_SEARCH = "https://www.google.com/maps/search/"
# Detail panels are ~1.5s each in the browser; three at a time keeps a 20-place
# request under the default timeout without hammering one exit.
DETAIL_CONCURRENCY = 3


def search_url(query: str, location: str | None) -> str:
    q = query.strip()
    if location and location.strip():
        q = f"{q} in {location.strip()}"
    return MAPS_SEARCH + quote_plus(q).replace("%2B", "+")


class PlacesService:
    def __init__(self, scrape_service: Any, *, persist: bool = True) -> None:
        self._scrape = scrape_service
        self._persist = persist

    async def search(self, req: PlacesSearchRequest) -> PlacesSearchData:
        url = search_url(req.query, req.location)
        page_options = ScrapeOptions(
            tier="browser",
            formats=["html"],
            onlyMainContent=False,
            timeout=min(req.timeout, 90_000),
        )
        outcome = await self._scrape.scrape(url, page_options)
        places = parse_results(outcome.data.html or "")[: req.limit]
        logger.info("places_search", query=req.query, location=req.location, found=len(places))

        detail_fetches = 0
        if req.includeDetails and places:
            detail_fetches = await self._add_details(places, req.timeout)

        enriched = 0
        if req.enrich and places:
            enriched = await self._enrich(places)

        if self._persist:
            await self._remember(places)

        return PlacesSearchData(
            query=req.query,
            location=req.location,
            places=places,
            search_pages=1,
            detail_fetches=detail_fetches,
            enriched=enriched,
        )

    # -- details -----------------------------------------------------------

    async def add_details(
        self, places: list[Place], timeout_ms: int, *, escalate: bool = False
    ) -> int:
        """Each place's detail panel (phone, website, address), in place. Returns
        how many panels were read.

        `escalate` gives a panel the browser cannot read one more try through
        a residential exit. Twenty panels in a row from one address is what
        Find Leads does, and Google starts answering those with empty pages.
        """
        return await self._add_details(places, timeout_ms, escalate=escalate)

    async def _add_details(
        self, places: list[Place], timeout_ms: int, *, escalate: bool = False
    ) -> int:
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
        # With `escalate`, a panel the browser cannot read is tried once more
        # through a residential exit; named rungs, because `auto` answered
        # Maps with a plain rung that cannot run the page (measured).
        tiers = ("browser", "stealth") if escalate else ("browser",)
        options = [
            ScrapeOptions(
                tier=tier, formats=["html"], onlyMainContent=False, timeout=min(timeout_ms, 60_000)
            )
            for tier in tiers
        ]
        fetched = 0

        async def one(i: int, place: Place) -> None:
            nonlocal fetched
            async with sem:
                out = None
                for attempt in options:
                    try:
                        out = await self._scrape.scrape(place.place_url, attempt)
                        break
                    except EngineError as exc:
                        logger.info(
                            "places_detail_failed",
                            feature_id=place.feature_id,
                            tier=attempt.tier,
                            code=str(exc.code),
                        )
                if out is None:
                    return
                fetched += 1
                d = parse_detail(out.data.html or "")
                places[i] = place.model_copy(
                    update={
                        "website": d.website or place.website,
                        "phone": d.phone or place.phone,
                        "address": d.address or place.address,
                        "rating": d.rating if d.rating is not None else place.rating,
                        "review_count": d.review_count
                        if d.review_count is not None
                        else place.review_count,
                    }
                )

        await asyncio.gather(*(one(i, p) for i, p in enumerate(places)))
        return fetched

    # -- enrichment through leadgen ----------------------------------------

    async def _enrich(self, places: list[Place]) -> int:
        try:
            from engine.leadgen.discovery import discover_contacts
        except ImportError:
            logger.info("places_enrich_unavailable", reason="leadgen not installed")
            return 0

        count = 0
        for i, place in enumerate(places):
            if not place.website:
                continue
            try:
                found = await discover_contacts(place.website, self._scrape)
            except Exception as exc:  # noqa: BLE001 - one bad website must not fail the list
                logger.info(
                    "places_enrich_failed", feature_id=place.feature_id, error=str(exc)[:80]
                )
                continue
            count += 1
            places[i] = place.model_copy(
                update={
                    "contacts": PlaceContacts(
                        emails=[e.address for e in found.emails],
                        contact_page_url=found.contact_page_url,
                        contact_form_url=found.contact_form_url,
                        social_links=dict(found.social_links),
                        status=str(found.status),
                    )
                }
            )
        return count

    # -- memory ------------------------------------------------------------

    async def _remember(self, places: list[Place]) -> None:
        """Keyed on feature id, so a repeat search updates rather than duplicates."""
        try:
            from engine.storage import repositories as repo
        except ImportError:
            return
        for place in places:
            with contextlib.suppress(Exception):
                await repo.upsert_place(place.model_dump())


__all__ = ["DETAIL_CONCURRENCY", "PlacesService", "search_url"]
