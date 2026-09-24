"""End-to-end crawl against real Postgres and Redis.

This is the Phase 2 acceptance criterion: a crawl completes, terminates
correctly, respects politeness, and survives a worker being killed mid-run.

Skipped automatically when the services are not reachable, so the unit suite
still runs anywhere.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from engine.core.fetch.base import FetchRequest, FetchResult
from engine.core.frontier.crawler import CrawlRunner
from engine.core.models import CrawlRequest, JobStatus, Tier
from engine.core.politeness import close_redis, get_redis
from engine.core.scrape_service import ScrapeService
from engine.settings import settings
from engine.storage import db
from engine.storage import repositories as repo
from engine.storage.ids import new_id

pytestmark = pytest.mark.integration


async def _services_available() -> bool:
    try:
        if not await db.healthy():
            return False
        client = await get_redis()
        return bool(await client.ping())
    except Exception:  # noqa: BLE001 - unavailable services skip, never fail
        return False


@pytest.fixture(scope="module", autouse=True)
async def _require_services() -> AsyncIterator[None]:
    os.environ.setdefault("ENGINE_DATABASE_URL", "postgresql://localhost:5432/scraping_engine")
    if not await _services_available():
        pytest.skip("Postgres and Redis are required for the integration suite")

    # The fixture site uses .invalid hosts, which by RFC 2606 never resolve —
    # so the SSRF guard rejects them before any fetch, exactly as designed.
    # Turning it off keeps this suite hermetic (no real DNS); the guard's own
    # behaviour is covered exhaustively in test_ssrf.py.
    previous = settings.ssrf_guard_enabled
    settings.ssrf_guard_enabled = False
    try:
        yield
    finally:
        settings.ssrf_guard_enabled = previous
        await db.close_pool()
        await close_redis()


# --------------------------------------------------------------------------
# A small fake site
# --------------------------------------------------------------------------

SITE: dict[str, str] = {
    "https://crawl.example.invalid/": """
        <html lang="en"><head><title>Home</title></head><body><article>
        <h1>Home</h1><p>The home page of a small test site with enough prose to
        register as genuine content for the extraction pipeline.</p>
        <a href="/a">Page A</a><a href="/b">Page B</a>
        <a href="/trap" style="display:none">trap</a>
        <a href="https://elsewhere.example.invalid/x">External</a>
        </article></body></html>""",
    "https://crawl.example.invalid/a": """
        <html lang="en"><head><title>Page A</title></head><body><article>
        <h1>Page A</h1><p>Body text for page A, long enough that extraction
        treats it as real content rather than a fragment of page furniture.</p>
        <a href="/c">Page C</a></article></body></html>""",
    "https://crawl.example.invalid/b": """
        <html lang="en"><head><title>Page B</title></head><body><article>
        <h1>Page B</h1><p>Body text for page B, again with sufficient prose to
        pass the extraction and validation thresholds comfortably.</p>
        </article></body></html>""",
    "https://crawl.example.invalid/c": """
        <html lang="en"><head><title>Page C</title></head><body><article>
        <h1>Page C</h1><p>Body text for page C, the deepest page reachable in
        this small fixture site used by the crawl integration test.</p>
        </article></body></html>""",
}


class SiteFetcher:
    """Serves the fixture site. Records what was requested."""

    name = "http"

    def __init__(self) -> None:
        self.requested: list[str] = []

    async def fetch(self, req: FetchRequest) -> FetchResult:
        self.requested.append(req.url)
        url = req.url.rstrip("/") if req.url != "https://crawl.example.invalid/" else req.url
        html = SITE.get(url) or SITE.get(url + "/")
        if html is None:
            return FetchResult(
                url=req.url,
                status_code=404,
                headers={},
                body=b"<html>Not found</html>",
                content_type="text/html",
                tier=self.name,
                latency_ms=5,
                bytes_transferred=30,
            )
        body = html.encode()
        return FetchResult(
            url=req.url,
            status_code=200,
            headers={},
            body=body,
            content_type="text/html; charset=utf-8",
            tier=self.name,
            latency_ms=10,
            bytes_transferred=len(body),
        )

    async def healthcheck(self) -> bool:
        return True


@pytest.fixture
async def crawl_job() -> AsyncIterator[tuple[str, CrawlRequest, SiteFetcher, CrawlRunner]]:
    key_plaintext = f"sk_it_{new_id('t')}"
    key_id = await repo.create_api_key(
        key_plaintext, "integration-test", owner_ref=f"it-owner-{new_id('o')}"
    )

    request = CrawlRequest(
        url="https://crawl.example.invalid/",
        limit=10,
        maxDepth=2,
        ignoreSitemap=True,
        respectRobots=False,
        allowBackwardLinks=True,
    )
    job_id = await repo.create_job("crawl", key_id, request.model_dump(mode="json"))

    fetcher = SiteFetcher()
    # persist=True so the frontier, pages and profiles all go through Postgres.
    service = ScrapeService({Tier.HTTP: fetcher}, persist=True)
    runner = CrawlRunner(service)

    yield job_id, request, fetcher, runner

    await db.execute("DELETE FROM jobs WHERE id = $1", job_id)
    # Usage rows first: a crawl now meters every page, so the key is referenced
    # from usage_events and deleting it outright is a foreign-key violation.
    for table in ("usage_daily_costs", "usage_daily", "usage_events"):
        await db.execute(f"DELETE FROM {table} WHERE api_key_id = $1", key_id)  # noqa: S608
    await db.execute("DELETE FROM api_keys WHERE id = $1", key_id)
    await db.execute("DELETE FROM domain_profiles WHERE domain LIKE '%example.invalid'")


async def _drain(runner: CrawlRunner, job_id: str, request: CrawlRequest, worker: str) -> int:
    processed = 0
    while await runner.process_one(job_id, request, worker):
        processed += 1
        if processed > 50:
            pytest.fail("crawl did not terminate")
    return processed


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


async def test_crawl_completes_and_terminates(crawl_job: Any) -> None:
    job_id, request, _, runner = crawl_job

    await runner.seed(job_id, request)
    processed = await _drain(runner, job_id, request, "worker-1")

    assert processed >= 3
    assert await runner.is_finished(job_id, request.limit)

    counts = await repo.frontier_counts(job_id)
    assert counts.get("pending", 0) == 0
    assert counts.get("claimed", 0) == 0


async def test_pages_are_stored_with_extracted_content(crawl_job: Any) -> None:
    job_id, request, _, runner = crawl_job

    await runner.seed(job_id, request)
    await _drain(runner, job_id, request, "worker-1")

    pages = await repo.list_job_pages(job_id, limit=50)
    titles = {page["title"] for page in pages if page["ok"]}
    assert "Home" in titles
    assert "Page A" in titles
    for page in pages:
        if page["ok"]:
            assert page["markdown"]
            assert page["extraction_confidence"] is not None


async def test_honeypot_and_external_links_are_recorded_as_skipped(crawl_job: Any) -> None:
    """Skipped URLs are auditable rather than silently dropped."""
    job_id, request, fetcher, runner = crawl_job

    await runner.seed(job_id, request)
    await _drain(runner, job_id, request, "worker-1")

    rows = await db.fetch(
        "SELECT url, skip_reason FROM frontier WHERE job_id = $1 AND status = 'skipped'",
        job_id,
    )
    reasons = {row["url"]: row["skip_reason"] for row in rows}
    assert any("trap" in url for url in reasons), "hidden link should be recorded as skipped"
    assert any("elsewhere" in url for url in reasons), "external link should be recorded"

    # And nothing skipped was ever actually fetched.
    assert not any("trap" in url for url in fetcher.requested)
    assert not any("elsewhere" in url for url in fetcher.requested)


async def test_dedup_prevents_refetching_the_same_url(crawl_job: Any) -> None:
    job_id, request, fetcher, runner = crawl_job

    await runner.seed(job_id, request)
    await _drain(runner, job_id, request, "worker-1")

    page_fetches = [u for u in fetcher.requested if "robots" not in u]
    assert len(page_fetches) == len(set(page_fetches)), "a URL was fetched twice"


async def test_limit_is_respected(crawl_job: Any) -> None:
    job_id, _, _, runner = crawl_job
    request = CrawlRequest(
        url="https://crawl.example.invalid/",
        limit=2,
        maxDepth=2,
        ignoreSitemap=True,
        respectRobots=False,
        allowBackwardLinks=True,
    )
    await runner.seed(job_id, request)
    await _drain(runner, job_id, request, "worker-1")

    counts = await repo.frontier_counts(job_id)
    assert counts.get("done", 0) <= 2


async def test_crawl_survives_a_worker_killed_mid_run(crawl_job: Any) -> None:
    """The Phase 2 acceptance criterion.

    A worker claims a URL and dies. Its claim is reaped back to pending and a
    second worker finishes the job with no lost or duplicated pages.
    """
    job_id, request, _, runner = crawl_job

    await runner.seed(job_id, request)
    await runner.process_one(job_id, request, "worker-doomed")

    # Simulate the crash: a claim left behind, never completed.
    claimed = await repo.claim_frontier_url(job_id, "worker-doomed")
    assert claimed is not None
    assert (await repo.frontier_counts(job_id)).get("claimed", 0) == 1

    # The reaper returns it (timeout 0 forces every claim stale).
    reaped = await repo.reap_stale_claims(timeout_seconds=0)
    assert reaped >= 1
    assert (await repo.frontier_counts(job_id)).get("claimed", 0) == 0

    # A second worker finishes cleanly.
    await _drain(runner, job_id, request, "worker-2")
    assert await runner.is_finished(job_id, request.limit)

    pages = await repo.list_job_pages(job_id, limit=50)
    urls = [page["url"] for page in pages]
    assert len(urls) == len(set(urls)), "re-execution duplicated a page row"


async def test_reaper_fails_a_url_after_the_attempt_ceiling(crawl_job: Any) -> None:
    job_id, request, _, runner = crawl_job
    await runner.seed(job_id, request)

    for _ in range(4):
        row = await repo.claim_frontier_url(job_id, "worker-flaky")
        if row is None:
            break
        await repo.reap_stale_claims(timeout_seconds=0, max_attempts=3)

    counts = await repo.frontier_counts(job_id)
    assert counts.get("failed", 0) >= 1, "a repeatedly-claimed URL must eventually fail"


async def test_job_counters_are_derived_from_frontier_state(crawl_job: Any) -> None:
    """Counters are derived rather than incremented, which removes a whole
    class of drift bugs when a worker is reaped mid-update."""
    job_id, request, _, runner = crawl_job

    await runner.seed(job_id, request)
    await _drain(runner, job_id, request, "worker-1")
    await repo.refresh_job_counters(job_id)

    job = await repo.get_job(job_id)
    counts = await repo.frontier_counts(job_id)
    assert job is not None
    assert job["total"] == sum(counts.values())
    assert job["completed"] == counts.get("done", 0)
    assert job["failed"] == counts.get("failed", 0)


async def test_cancelled_job_stops_and_stays_terminal(crawl_job: Any) -> None:
    job_id, request, _, runner = crawl_job
    await runner.seed(job_id, request)

    await repo.set_job_status(job_id, JobStatus.CANCELLED)
    job = await repo.get_job(job_id)
    assert job is not None and job["status"] == "cancelled"

    # Terminal states are immutable: a completed job never reopens.
    await repo.set_job_status(job_id, JobStatus.RUNNING)
    job = await repo.get_job(job_id)
    assert job is not None and job["status"] == "cancelled"


async def test_domain_profile_learns_from_the_crawl(crawl_job: Any) -> None:
    job_id, request, _, runner = crawl_job
    await runner.seed(job_id, request)
    await _drain(runner, job_id, request, "worker-1")

    profile = await repo.load_domain_profile("example.invalid")
    assert profile.success_count >= 3
    assert profile.avg_content_length is not None
    assert profile.min_working_tier == Tier.HTTP


async def test_cache_serves_a_repeat_scrape_without_refetching(crawl_job: Any) -> None:
    """The cache IS the pages table — a hit costs nothing."""
    job_id, request, fetcher, runner = crawl_job
    await runner.seed(job_id, request)
    await _drain(runner, job_id, request, "worker-1")

    before = len(fetcher.requested)
    from engine.core.models import ScrapeOptions

    service = ScrapeService({Tier.HTTP: fetcher}, persist=True)
    outcome = await service.scrape(
        "https://crawl.example.invalid/a", ScrapeOptions(maxAge=600_000, respectRobots=False)
    )

    assert outcome.from_cache
    assert outcome.data.cost.cached is True
    assert outcome.data.cost.proxy_bytes == 0
    assert len(fetcher.requested) == before, "a cache hit must not fetch"


async def test_politeness_is_enforced_between_requests(crawl_job: Any) -> None:
    """The delay is enforced through Redis so it holds across every worker,
    not just within one process."""
    from engine.core.politeness import PolitenessGate

    gate = PolitenessGate()
    domain = "politeness.example.invalid"

    # Start from a known queue. The ticket counter lives in Redis and survives
    # between tests, so asserting exact waits without clearing first passes
    # alone and fails in the suite — which is how this was found.
    client = await get_redis()
    await client.delete(f"politeness:next:{domain}", f"politeness:slots:{domain}")

    first = await gate.acquire(domain, delay_ms=1_000, max_concurrency=2)
    assert first.allowed
    await gate.release(domain)

    # The gate hands out TICKETS: the second caller is not turned away, it is
    # told when its turn is. The guarantee is the same — a full delay passes
    # between the two requests — and asserting the wait is stronger than
    # asserting a refusal, which said nothing about how long.
    second = await gate.acquire(domain, delay_ms=1_000, max_concurrency=2)
    assert second.allowed, "the second caller should be queued, not refused"
    assert second.wait_ms is not None
    assert 900 <= second.wait_ms <= 1_000, "the per-domain delay was not enforced"

    # A third waits two delays: the queue is ordered, not a scramble.
    third = await gate.acquire(domain, delay_ms=1_000, max_concurrency=2)
    assert third.allowed and third.wait_ms is not None
    assert 1_900 <= third.wait_ms <= 2_000, "tickets must not collide"

    # And a caller that cannot wait that long takes no ticket, so it does not
    # push the queue out for anyone behind it.
    impatient = await gate.acquire(domain, delay_ms=1_000, max_concurrency=8, max_wait_ms=10)
    assert not impatient.allowed and impatient.reason == "queue_too_long"
    after = await gate.acquire(domain, delay_ms=1_000, max_concurrency=8)
    assert after.wait_ms is not None and after.wait_ms <= 3_000, "a refusal moved the queue"

    await client.delete(f"politeness:next:{domain}", f"politeness:slots:{domain}")


async def test_concurrency_cap_holds_across_workers() -> None:
    from engine.core.politeness import PolitenessGate

    gate = PolitenessGate()
    domain = "concurrency.example.invalid"
    client = await get_redis()
    await client.delete(f"politeness:next:{domain}", f"politeness:slots:{domain}")

    # Two slots available, third caller is refused.
    a = await gate.acquire(domain, delay_ms=0, max_concurrency=2)
    await client.delete(f"politeness:next:{domain}")
    b = await gate.acquire(domain, delay_ms=0, max_concurrency=2)
    await client.delete(f"politeness:next:{domain}")
    c = await gate.acquire(domain, delay_ms=0, max_concurrency=2)

    assert a.allowed and b.allowed
    assert not c.allowed and c.reason == "max_concurrency"

    await gate.release(domain)
    await gate.release(domain)
    await client.delete(f"politeness:next:{domain}", f"politeness:slots:{domain}")


async def test_release_never_drives_the_slot_counter_negative() -> None:
    """A double release must not free capacity that was never taken."""
    from engine.core.politeness import PolitenessGate

    gate = PolitenessGate()
    domain = "release.example.invalid"
    client = await get_redis()
    await client.delete(f"politeness:slots:{domain}")

    await gate.release(domain)
    await gate.release(domain)

    value = await client.get(f"politeness:slots:{domain}")
    assert value is None or int(value) >= 0


async def test_concurrent_workers_never_claim_the_same_url(crawl_job: Any) -> None:
    """Claiming is one atomic statement with FOR UPDATE SKIP LOCKED — no
    advisory locks, no select-then-update race."""
    job_id, request, _, runner = crawl_job
    await runner.seed(job_id, request)

    claims = await asyncio.gather(
        *[repo.claim_frontier_url(job_id, f"worker-{i}") for i in range(5)]
    )
    ids = [row["id"] for row in claims if row is not None]
    assert len(ids) == len(set(ids)), "two workers claimed the same frontier row"


async def test_a_crawl_that_hits_its_limit_leaves_no_url_unbucketed(crawl_job: Any) -> None:
    """Field report §27: the leftover pending URLs when a crawl stops at its
    limit are counted in `total` but in no terminal bucket, so the run does not
    reconcile. They land in `skipped` with a reason instead."""
    job_id, _request, _fetcher, _runner = crawl_job

    import hashlib

    async def add(url: str, status: str) -> None:
        h = hashlib.sha256(url.encode()).digest()
        await db.execute(
            """
            INSERT INTO frontier
                (job_id, url, url_hash, normalized_hash, depth, status)
            VALUES ($1, $2, $3, $4, 0, $5::frontier_status)
            """,
            job_id,
            url,
            h,
            h,
            status,
        )

    for i in range(3):
        await add(f"https://crawl.example.invalid/done{i}", "done")
    await add("https://crawl.example.invalid/failed0", "failed")
    for i in range(4):
        await add(f"https://crawl.example.invalid/pending{i}", "pending")
    await add("https://crawl.example.invalid/claimed0", "claimed")

    moved = await repo.skip_pending_frontier(job_id, "limit_reached")
    assert moved == 5, "four pending and one claimed"

    await repo.refresh_job_counters(job_id)
    job = await repo.get_job(job_id)
    assert job["completed"] == 3 and job["failed"] == 1 and job["skipped"] == 5
    assert job["completed"] + job["failed"] + job["skipped"] == job["total"], (
        "every discovered URL is in exactly one bucket, and they sum to total"
    )

    counts = await repo.frontier_counts(job_id)
    assert counts.get("pending", 0) == 0 and counts.get("claimed", 0) == 0
    reason = await db.fetchval(
        "SELECT skip_reason FROM frontier WHERE job_id=$1 AND url LIKE '%pending0'", job_id
    )
    assert reason == "limit_reached"


async def test_skipping_the_frontier_leaves_done_and_failed_untouched(crawl_job: Any) -> None:
    job_id, _request, _fetcher, _runner = crawl_job
    await db.execute(
        """
        INSERT INTO frontier (job_id, url, url_hash, normalized_hash, depth, status)
        VALUES ($1, 'https://crawl.example.invalid/d', $2, $2, 0, 'done'::frontier_status)
        """,
        job_id,
        __import__("hashlib").sha256(b"d").digest(),
    )
    moved = await repo.skip_pending_frontier(job_id, "limit_reached")
    assert moved == 0, "nothing pending: a completed crawl's finish is a no-op here"
    status = await db.fetchval("SELECT status FROM frontier WHERE job_id=$1", job_id)
    assert status == "done"
