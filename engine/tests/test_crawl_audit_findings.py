"""Findings from a live audit crawl (measured, Sep 2026), pinned.

Real use found nine things the fixture suite could not. Each test here is one
of them, written so the same lie cannot come back: the tally that did not sum,
the cached page that vanished from its own crawl, the relative cursor, the
Cloudflare endpoints in `links`, the cost object that forgot its tier, the
worker that could not climb, the extractor that kept 9% of a page, and the URL
key that sat in a different place on every endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from engine.api.routes import crawl as crawl_routes
from engine.core.extract.heuristic import MIN_ACCEPTABLE_WORDS, under_extracted
from engine.core.extract.router import COVERAGE_FLOOR, collapse_repeats
from engine.core.frontier.crawler import _policy_for
from engine.core.frontier.discovery import extract_links
from engine.core.models import Cost, CrawlRequest, PageMetadata, Tier
from engine.core.scrape_service import ScrapeService


class Record(dict):
    """asyncpg.Record's surface that the payload code touches: `in` and `[]`."""


class FakeRequest:
    def __init__(self, base: str) -> None:
        self.base_url = base


# --------------------------------------------------------------------------
# §1 the tally, §5 the cursor
# --------------------------------------------------------------------------


def test_the_job_payload_reports_all_three_buckets_and_they_sum() -> None:
    job = Record(
        id="crawl_x",
        kind="crawl",
        status="completed",
        total=54,
        completed=44,
        failed=2,
        skipped=8,
        cost={},
        started_at=None,
        completed_at=None,
        input={"url": "https://example.com"},
        error=None,
    )
    payload = crawl_routes._job_payload(job, "https://api.snoop.test")
    assert payload["skipped"] == 8
    assert payload["completed"] + payload["failed"] + payload["skipped"] == payload["total"]


def test_a_job_row_from_before_the_column_still_renders() -> None:
    job = Record(
        id="crawl_old",
        kind="crawl",
        status="completed",
        total=3,
        completed=3,
        failed=0,
        cost={},
        started_at=None,
        completed_at=None,
        input={},
        error=None,
    )
    assert crawl_routes._job_payload(job, "https://api.snoop.test")["skipped"] == 0


def test_next_links_are_absolute_urls() -> None:
    job = Record(
        id="crawl_x",
        kind="crawl",
        status="running",
        total=1,
        completed=0,
        failed=0,
        skipped=0,
        cost={},
        started_at=None,
        completed_at=None,
        input={},
        error=None,
    )
    base = crawl_routes._base(FakeRequest("https://api.snoop.test/"))
    assert base == "https://api.snoop.test"
    payload = crawl_routes._job_payload(job, base)
    assert payload["next"] == "https://api.snoop.test/v1/crawl/crawl_x/pages"


# --------------------------------------------------------------------------
# "Also": one metadata object, wherever a page comes back
# --------------------------------------------------------------------------


def _page_row(**overrides: Any) -> Record:
    row = Record(
        id="page_1",
        url="https://example.com/about-us",
        source_url="https://example.com/about-us/",
        ok=True,
        error_code=None,
        markdown="# About",
        title="About",
        description=None,
        language="en",
        author=None,
        published_at=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        status_code=200,
        content_type="text/html",
        page_type="unknown",
        word_count=324,
        extraction_confidence=0.8,
    )
    row.update(overrides)
    return row


def test_a_crawl_page_carries_the_scrape_metadata_object() -> None:
    payload = crawl_routes._page_payload(_page_row())
    meta = payload["metadata"]
    assert set(meta) == set(PageMetadata.model_fields), "crawl and scrape share one shape"
    assert meta["sourceURL"] == "https://example.com/about-us/"
    assert meta["url"] == "https://example.com/about-us"
    assert meta["publishedAt"] == "2026-09-05T12:00:00+00:00"
    # The keys clients already read still answer.
    assert payload["url"] == meta["url"] and payload["sourceURL"] == meta["sourceURL"]


def test_a_failed_page_row_still_renders() -> None:
    row = _page_row(
        ok=False,
        error_code="BLOCKED",
        markdown=None,
        title=None,
        source_url=None,
        published_at=None,
        page_type=None,
        word_count=None,
        extraction_confidence=None,
    )
    meta = crawl_routes._page_payload(row)["metadata"]
    assert meta["sourceURL"] == "https://example.com/about-us", "no source_url: the url stands in"
    assert meta["pageType"] == "unknown" and meta["wordCount"] == 0
    assert meta["publishedAt"] is None


# --------------------------------------------------------------------------
# §4 a cached page belongs to the job that hit it
# --------------------------------------------------------------------------


async def test_a_cache_hit_inside_a_job_is_linked_to_that_job(monkeypatch: Any) -> None:
    from engine.storage import repositories as repo

    stored: list[dict[str, Any]] = []

    async def fake_store(record: dict[str, Any]) -> str:
        stored.append(record)
        return "page_new"

    monkeypatch.setattr(repo, "store_page", fake_store)

    cached = {
        "id": "page_old",
        "url": "https://example.com/",
        "markdown": "# Home",
        "html": None,
        "raw_html": None,
        "links": [],
        "structured": None,
        "title": "Acme",
        "description": None,
        "language": "en",
        "author": None,
        "published_at": None,
        "page_type": "unknown",
        "word_count": 120,
        "extraction_confidence": 0.8,
        "extraction_path": "heuristic",
        "fetch_tier": "http",
        "tiers_attempted": ["http"],
        "proxy_type": None,
        "proxy_bytes": 0,
        "browser_ms": 0,
        "ok": True,
        "block_signals": None,
        "status_code": 200,
        "content_type": "text/html",
        "content_hash": "abc",
        "variant_hash": "v",
        "shared_cacheable": True,
        "job_id": "crawl_other",  # the row that served the hit belonged to someone else
    }
    service = ScrapeService({}, persist=False)
    await service._link_cached_page(cached, "crawl_mine", "https://example.com")

    assert len(stored) == 1
    row = stored[0]
    assert row["job_id"] == "crawl_mine", "the copy is this job's row, not the original's"
    assert row["source_url"] == "https://example.com"
    assert row["markdown"] == "# Home" and row["fetch_tier"] == "http"
    assert "id" not in row, "a fresh id is minted by store_page, never copied"


async def test_a_failed_link_never_fails_the_fetch(monkeypatch: Any) -> None:
    from engine.storage import repositories as repo

    async def boom(record: dict[str, Any]) -> str:
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "store_page", boom)
    service = ScrapeService({}, persist=False)
    await service._link_cached_page(
        {"url": "https://x.test/", "markdown": "x"}, "crawl_x", "https://x.test/"
    )


# --------------------------------------------------------------------------
# §6 Cloudflare's own endpoints are not links
# --------------------------------------------------------------------------


def test_cdn_cgi_anchors_are_screened_at_the_anchor() -> None:
    html = (
        '<a href="/cdn-cgi/l/email-protection#abc">email</a>'
        '<a href="https://example.com/cdn-cgi/challenge-platform/h/b">x</a>'
        '<a href="/services">Services</a>'
    )
    policy = _policy_for(CrawlRequest(url="https://example.com"))
    found = extract_links(html, "https://example.com/", policy, depth=0)
    urls = [link.url for link in found]
    assert urls == ["https://example.com/services"]


# --------------------------------------------------------------------------
# §8 a cache hit remembers what the page cost to fetch
# --------------------------------------------------------------------------


def test_a_cached_cost_carries_the_original_tier() -> None:
    cost = Cost.from_cache(tier="browser")
    assert cost.cached is True and cost.tier == "browser"
    assert Cost.from_cache().tier is None


# --------------------------------------------------------------------------
# the worker climbs only the rungs it is told to
# --------------------------------------------------------------------------


def test_worker_tiers_setting_selects_from_the_wired_ladder(monkeypatch: Any) -> None:
    from engine.api import deps
    from engine.settings import settings
    from engine.workers.http_worker import _worker_fetchers

    fake_ladder = {Tier.HTTP: "h", Tier.IMPERSONATE: "i", Tier.BROWSER: "b", Tier.STEALTH: "s"}
    monkeypatch.setattr(deps, "get_fetchers", lambda: fake_ladder)

    monkeypatch.setattr(settings, "worker_tiers", "http,impersonate,browser")
    assert set(_worker_fetchers()) == {Tier.HTTP, Tier.IMPERSONATE, Tier.BROWSER}

    monkeypatch.setattr(settings, "worker_tiers", "http,impersonate")
    assert set(_worker_fetchers()) == {Tier.HTTP, Tier.IMPERSONATE}


def test_worker_falls_back_to_tiers_0_and_1_when_nothing_named_is_wired(monkeypatch: Any) -> None:
    from engine.api import deps
    from engine.settings import settings
    from engine.workers.http_worker import _worker_fetchers

    monkeypatch.setattr(deps, "get_fetchers", lambda: {Tier.HTTP: "h"})
    monkeypatch.setattr(settings, "worker_tiers", "stealth_hard")
    chosen = _worker_fetchers()
    assert set(chosen) == {Tier.HTTP, Tier.IMPERSONATE}, (
        "a worker that can fetch nothing is not a worker"
    )


# --------------------------------------------------------------------------
# §3 the extractor keeps enough of the page, once
# --------------------------------------------------------------------------


def test_the_fallback_gate_is_relative_to_the_page() -> None:
    # 70 words cleared the old absolute floor; on a 740-word page that is 9%.
    assert under_extracted(70, raw_len=740 * 6)
    # The same 70 words on a 100-word page are the page.
    assert not under_extracted(70, raw_len=100 * 6)
    # Below the absolute floor is always under-extracted on a real page…
    assert under_extracted(MIN_ACCEPTABLE_WORDS - 1, raw_len=5_000)
    # …and a tiny page is never judged at all.
    assert not under_extracted(3, raw_len=200)
    assert 0 < COVERAGE_FLOOR < 1


def test_exact_repeated_blocks_collapse_and_order_holds() -> None:
    who = "## Who We Are\n\nWe do the work."
    md = "\n\n".join([who, who, "## Values\n\nHonesty.", who])
    out = collapse_repeats(md)
    assert out == "## Who We Are\n\nWe do the work.\n\n## Values\n\nHonesty."
    assert collapse_repeats("") == ""
