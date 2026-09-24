"""Crawl execution — seeding the frontier and processing claimed URLs.

The frontier lives in Postgres (02-data-model.md s3), so a worker can die at
any point without losing work: its claims are reaped back to `pending` and
re-executed, and every write is idempotent.

A crawl always terminates: no pending rows, or `limit` reached, or cancelled,
or the job timeout expires. An unbounded crawl is a bug, not a feature.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from engine.core import robots as robots_mod
from engine.core.errors import EngineError
from engine.core.fetch.base import FetchRequest
from engine.core.frontier.discovery import (
    CrawlPolicy,
    DiscoveredLink,
    default_sitemap_urls,
    extract_links,
    parse_sitemap,
    sitemap_urls_from_robots,
)
from engine.core.models import Cost, CrawlRequest, ScrapeOptions, Tier
from engine.core.scrape_service import ScrapeService
from engine.core.urls import host_of, normalized_hash, url_hash
from engine.storage import repositories as repo

logger = structlog.get_logger(__name__)

# A sitemap index can point at further indexes. Bound the recursion — a
# malicious or misconfigured site can otherwise loop for ever.
MAX_SITEMAP_DEPTH = 3
MAX_SITEMAPS = 50


@dataclass
class SeedResult:
    seeded: int
    skipped: int
    source: str


class CrawlRunner:
    """Seeds and advances one crawl job."""

    def __init__(self, service: ScrapeService) -> None:
        self._service = service
        # job_id -> the plan's concurrency, so per-host pacing scales with what
        # the job's owner pays for. See _plan_concurrency.
        self._plan_cache: dict[str, int | None] = {}

    # -- seeding -----------------------------------------------------------

    async def seed(self, job_id: str, request: CrawlRequest) -> SeedResult:
        policy = _policy_for(request)
        links: list[DiscoveredLink] = [DiscoveredLink(request.url, 0, None, "seed", None)]
        source = "seed"

        if not request.ignoreSitemap:
            sitemap_links = await self._from_sitemaps(request.url, policy, request.limit)
            if sitemap_links:
                links.extend(sitemap_links)
                source = "sitemap"

        rows = [link.to_frontier_row(drop_query=request.ignoreQueryParameters) for link in links]
        await repo.add_frontier_urls(job_id, rows)
        await repo.refresh_job_counters(job_id)

        skipped = sum(1 for link in links if link.skip_reason)
        return SeedResult(seeded=len(links) - skipped, skipped=skipped, source=source)

    async def _from_sitemaps(
        self, root_url: str, policy: CrawlPolicy, limit: int
    ) -> list[DiscoveredLink]:
        """Sitemap-first discovery. Cheaper, faster and more complete than
        finding the same URLs by crawling."""
        candidates: list[str] = []

        host = host_of(root_url)
        robots = await repo.cached_robots(host)
        if robots is None:
            # Declared sitemaps live in robots.txt; only the cached copy was
            # read, so a fresh domain's crawl seeded from a guess.
            robots = await self._fetch_text(f"{root_url.rstrip('/')}/robots.txt") or ""
            await repo.store_robots(host, robots)
        if robots:
            candidates.extend(sitemap_urls_from_robots(robots))
        candidates.extend(default_sitemap_urls(root_url))

        found: list[DiscoveredLink] = []
        seen_sitemaps: set[str] = set()
        queue = [(url, 0) for url in candidates]
        fetched = 0

        while queue and len(found) < limit and fetched < MAX_SITEMAPS:
            sitemap_url, depth = queue.pop(0)
            if sitemap_url in seen_sitemaps or depth > MAX_SITEMAP_DEPTH:
                continue
            seen_sitemaps.add(sitemap_url)

            body = await self._fetch_text(sitemap_url)
            fetched += 1
            if body is None:
                continue

            page_urls, nested = parse_sitemap(body)
            for nested_url in nested:
                queue.append((nested_url, depth + 1))

            for url in page_urls:
                if len(found) >= limit:
                    break
                reason = policy.evaluate(url, 1)
                found.append(DiscoveredLink(url, 1, sitemap_url, "sitemap", reason))

        return found

    async def _fetch_text(self, url: str) -> str | None:
        fetcher = self._service._fetchers.get(Tier.HTTP)
        if fetcher is None:
            return None
        try:
            result = await fetcher.fetch(FetchRequest(url=url, timeout_ms=15_000))
        except Exception as exc:  # noqa: BLE001 - a bad sitemap must not fail the crawl
            logger.warning("sitemap_fetch_failed", url=url, error=str(exc))
            return None
        if result.status_code != 200 or not result.body:
            return None
        return result.text(limit=10_000_000)

    # -- processing --------------------------------------------------------

    async def process_one(
        self,
        job_id: str,
        request: CrawlRequest,
        worker: str,
    ) -> bool:
        """Claim and process a single frontier URL.

        Returns False when there is nothing left to claim, which is how the
        worker loop learns the crawl is finished.
        """
        counts = await repo.frontier_counts(job_id)
        done = counts.get("done", 0)
        if done >= request.limit:
            return False

        row = await repo.claim_frontier_url(job_id, worker)
        if row is None:
            return False

        url = row["url"]
        depth = row["depth"]
        options = _scrape_options_for(request)

        try:
            outcome = await self._service.scrape(
                url,
                options,
                job_id=job_id,
                worker=worker,
                plan_concurrency=await self._plan_concurrency(job_id),
                owner_ref=await self._owner_ref(job_id),
            )
        except EngineError as exc:
            logger.info(
                "crawl_page_failed",
                job_id=job_id,
                url=url,
                code=str(exc.code),
                message=exc.message,
            )
            await self._record_failure(job_id, url, exc)
            await repo.complete_frontier_url(row["id"], ok=False)
            await repo.refresh_job_counters(job_id)
            return True

        # Discover further URLs from the page we just fetched.
        if depth < request.maxDepth and outcome.data.markdown is not None:
            await self._discover_from(job_id, request, url, depth, outcome)

        await repo.complete_frontier_url(row["id"], ok=True)
        await repo.refresh_job_counters(job_id)
        # One line per page with its terminal state, so the log can reconcile
        # the tally. With only seeded/failed/completed emitted, a silent success
        # and a silent loss looked identical (measured).
        logger.info(
            "crawl_page_done",
            job_id=job_id,
            url=url,
            depth=depth,
            tier=outcome.data.cost.tier,
            cached=outcome.from_cache,
            words=outcome.data.metadata.wordCount,
        )
        # Meter BEFORE accumulating: billing.charge is what fills cost.credits.
        await self._meter(job_id, url, outcome.data.cost)
        await repo.accumulate_job_cost(
            job_id,
            outcome.data.cost.proxy_bytes,
            outcome.data.cost.browser_ms,
            outcome.data.cost.tier or "unknown",
            outcome.data.cost.credits or 0,
        )
        return True

    async def _plan_concurrency(self, job_id: str) -> int | None:
        """The plan behind this job, looked up once and remembered.

        Per-page it would be a database round trip for every URL in the crawl,
        which is the hot loop; a job's plan does not change mid-run.
        """
        # Created lazily: these objects are also built with __new__ (the suite
        # skips Redis and the database that way), so __init__ has not always run.
        # Annotated explicitly: getattr's default-argument form returns Any,
        # which would otherwise poison the `return cache[job_id]` below into
        # an unchecked Any against this function's declared int | None.
        cache: dict[str, int | None] = getattr(self, "_plan_cache", None) or {}
        self._plan_cache = cache
        if job_id in cache:
            return cache[job_id]
        key = await repo.job_api_key(job_id)
        plan = getattr(key, "concurrency", None) if key is not None else None
        # Bounded: a long-lived worker sees many jobs and this must not grow
        # without limit.
        if len(cache) > 512:
            cache.clear()
            self._owner_cache = {}
        cache[job_id] = plan
        # Same lookup, so the owner comes free. A crawl that stored its pages
        # unowned would charge its OWN customer to re-read them later.
        owners = getattr(self, "_owner_cache", None)
        if owners is None:
            owners = {}
            self._owner_cache = owners
        owners[job_id] = getattr(key, "owner_ref", None) if key is not None else None
        return plan

    async def _owner_ref(self, job_id: str) -> str | None:
        owners = getattr(self, "_owner_cache", None) or {}
        if job_id not in owners:
            await self._plan_concurrency(job_id)
            owners = getattr(self, "_owner_cache", None) or {}
        return owners.get(job_id)

    async def _meter(self, job_id: str, url: str, cost: Cost) -> None:
        """Charge the job's key for one crawled page; operator keys are never metered."""
        from engine.api import billing

        key = await repo.job_api_key(job_id)
        if key is not None:
            await billing.charge(key, endpoint="crawl", url=url, cost=cost, job_id=job_id)

    async def _discover_from(
        self,
        job_id: str,
        request: CrawlRequest,
        url: str,
        depth: int,
        outcome: Any,
    ) -> None:
        html = outcome.data.html or ""
        if not html:
            # _scrape_options_for always requests html precisely so this branch
            # does not run. If it ever does, the anchors are gone and honeypot
            # detection is impossible — so discovery stops rather than
            # following links we could not screen.
            logger.warning(
                "link_discovery_skipped_no_html",
                job_id=job_id,
                url=url,
                reason="honeypot screening needs the source anchors",
            )
            return

        discovered = extract_links(html, url, _policy_for(request), depth=depth)

        if not discovered:
            return

        rows = [
            link.to_frontier_row(drop_query=request.ignoreQueryParameters) for link in discovered
        ]
        await repo.add_frontier_urls(job_id, rows)

    async def _record_failure(self, job_id: str, url: str, exc: EngineError) -> None:
        try:
            await repo.store_page(
                {
                    "job_id": job_id,
                    "url": url,
                    "source_url": url,
                    "normalized_hash": normalized_hash(url),
                    "ok": False,
                    "error_code": str(exc.code),
                    "block_signals": exc.detail or None,
                }
            )
        except Exception as store_exc:  # noqa: BLE001 - never mask the original failure
            logger.warning("failure_record_failed", url=url, error=str(store_exc))

    # -- termination -------------------------------------------------------

    async def is_finished(self, job_id: str, limit: int) -> bool:
        counts = await repo.frontier_counts(job_id)
        if counts.get("done", 0) >= limit:
            return True
        return counts.get("pending", 0) == 0 and counts.get("claimed", 0) == 0


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _policy_for(request: CrawlRequest) -> CrawlPolicy:
    return CrawlPolicy(
        root_url=request.url,
        max_depth=request.maxDepth,
        include_paths=tuple(request.includePaths),
        exclude_paths=tuple(request.excludePaths),
        allow_external_links=request.allowExternalLinks,
        allow_backward_links=request.allowBackwardLinks,
        ignore_query_parameters=request.ignoreQueryParameters,
    )


def _scrape_options_for(request: CrawlRequest) -> ScrapeOptions:
    options = request.scrapeOptions.model_copy()
    formats = list(options.formats)

    # Links advance the frontier even when the caller did not ask for them.
    if not options._has_format("links"):
        formats.append("links")

    # HTML is required, not optional: honeypot detection reads the ANCHOR
    # (hidden styling, zero-size, aria-hidden), and a bare list of URLs carries
    # none of that. Without it a default crawl would happily follow the hidden
    # links Cloudflare's AI Labyrinth plants, which is how a crawler announces
    # itself as a bot and gets its whole session downgraded.
    if not options._has_format("html"):
        formats.append("html")

    options.formats = formats
    options.respectRobots = request.respectRobots
    return options


async def robots_permits(url: str, user_agent: str) -> bool:
    host = host_of(url)
    body = await repo.cached_robots(host)
    if body is None:
        return True
    rules = robots_mod.parse(body, user_agent)
    return rules.permits(robots_mod.path_of(url))


__all__ = ["CrawlRunner", "SeedResult", "url_hash"]
