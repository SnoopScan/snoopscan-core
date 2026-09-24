"""Crawl, map and batch endpoints (01-api-surface.md).

Asynchronous: these return a job id immediately and never block on the work.
Crawl results are cursor-paginated and never inlined — a 10,000-page crawl
must not be a single JSON body.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import structlog
from fastapi import APIRouter, Query, Request
from pydantic import ValidationError

from engine.api import billing
from engine.api.deps import ApiKeyDep, ServiceDep
from engine.api.js_gate import guard_js_execution
from engine.core.errors import EngineError, JobNotFound
from engine.core.frontier.discovery import (
    CrawlPolicy,
    default_sitemap_urls,
    parse_sitemap,
    sitemap_urls_from_robots,
    title_from_html,
)
from engine.core.models import (
    BatchScrapeRequest,
    Cost,
    CrawlRequest,
    JobStatus,
    MapRequest,
    ScrapeOptions,
    Tier,
)
from engine.core.scrape_service import formats_from_page_row, page_metadata_from_row
from engine.core.ssrf import resolve_and_validate
from engine.core.urls import host_of, normalize_url
from engine.storage import repositories as repo
from engine.workers.queue import JobMessage, JobQueue, Queue

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["crawl"])
_queue = JobQueue()

MAX_PAGE_LIMIT = 200
DEFAULT_PAGE_LIMIT = 50


# --------------------------------------------------------------------------
# Crawl
# --------------------------------------------------------------------------


@router.post("/crawl")
async def start_crawl(body: CrawlRequest, key: ApiKeyDep) -> dict[str, Any]:
    guard_js_execution(body, key.allow_js_exec)
    # A queued job bills per page as it runs, so the gate belongs at submission
    # too: without it a suspended account, or one on zero credits, could queue a
    # crawl and have it served in full. Every synchronous route already gates.
    billing.assert_credits(key)

    await resolve_and_validate(body.url)

    # What the caller can actually pay for. A crawl bills per page as it runs,
    # so a limit of 10,000 against a balance of 5 used to be accepted in full
    # and die part-done with nothing said at submission. The limit that fits is
    # honoured as asked; one that does not is lowered to what the balance
    # covers; a balance covering no pages at all is a 402 right here, before
    # anything is queued.
    # `auto` is the default and usually resolves to a direct fetch, so quoting
    # it at the proxied rate would halve every ordinary crawl's ceiling for a
    # cost most of them never incur. Only an EXPLICIT proxy is a guarantee of
    # one, and only that is priced as one.
    requested_proxy = str(getattr(body.scrapeOptions, "proxy", "") or "").lower()
    per_page = await billing.page_price(proxied=requested_proxy not in ("", "none", "auto"))
    allowed = await billing.affordable_limit(key, body.limit, per_page=per_page)

    payload = body.model_dump(mode="json")
    payload["limit"] = allowed

    job_id = await repo.create_job(
        "crawl",
        key.id,
        payload,
        webhook_url=body.webhook.url if body.webhook else None,
        webhook_events=list(body.webhook.events) if body.webhook else None,
    )
    await _queue.push(JobMessage(job_id=job_id, kind="crawl"), Queue.FETCH_HTTP)

    return {
        "success": True,
        "data": {
            "id": job_id,
            "status": str(JobStatus.QUEUED),
            "url": body.url,
            # Say the ceiling out loud, and say so when it was lowered — a
            # caller that asked for 10,000 and got 40 needs to know now.
            "limit": allowed,
            "limitRequested": body.limit,
            "creditsPerPage": per_page,
            "creditsMax": allowed * per_page,
        },
    }


@router.get("/crawl/{job_id}")
async def crawl_status(job_id: str, key: ApiKeyDep, request: Request) -> dict[str, Any]:
    job = await _load_job(job_id, key.id)
    warning = await _job_warning(job)
    return {"success": True, "data": _job_payload(job, _base(request), warning)}


@router.get("/crawl/{job_id}/pages")
async def crawl_pages(
    request: Request,
    job_id: str,
    key: ApiKeyDep,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
) -> dict[str, Any]:
    job = await _load_job(job_id, key.id)
    after_id = _decode_cursor(cursor)

    rows = await repo.list_job_pages(job_id, after_id=after_id, limit=limit)
    options = _requested_options(job)
    pages = [_page_payload(row, options) for row in rows]

    next_cursor = None
    if len(rows) == limit:
        next_cursor = _encode_cursor(rows[-1]["id"])

    return {
        "success": True,
        "data": {
            "pages": pages,
            "next": (
                f"{_base(request)}/v1/crawl/{job_id}/pages?cursor={next_cursor}"
                if next_cursor
                else None
            ),
        },
    }


@router.get("/crawl/{job_id}/errors")
async def crawl_errors(job_id: str, key: ApiKeyDep) -> dict[str, Any]:
    """The pages that failed, and why.

    Firecrawl exposes this separately from the page results, and it is the
    right split: a caller reading results should not have to filter failures
    out of them, and a caller debugging a crawl wants only the failures.
    """
    await _load_job(job_id, key.id)
    rows = await repo.list_job_pages(job_id, limit=MAX_PAGE_LIMIT)
    failures = [
        {
            "id": row["id"],
            "url": row["url"],
            "error": row["error_code"],
            "signals": row["block_signals"],
            "at": _iso(row["fetched_at"]),
        }
        for row in rows
        if not row["ok"]
    ]
    return {"success": True, "data": {"errors": failures, "count": len(failures)}}


@router.delete("/crawl/{job_id}")
async def cancel_crawl(job_id: str, key: ApiKeyDep, request: Request) -> dict[str, Any]:
    job = await _load_job(job_id, key.id)
    # Terminal states are immutable — a completed job never reopens.
    if job["status"] in ("completed", "failed", "cancelled"):
        return {"success": True, "data": _job_payload(job, _base(request))}

    await repo.set_job_status(job_id, JobStatus.CANCELLED)
    final = await repo.get_job(job_id)
    assert final is not None
    return {"success": True, "data": _job_payload(final, _base(request))}


# --------------------------------------------------------------------------
# Batch
# --------------------------------------------------------------------------


@router.post("/batch/scrape")
async def start_batch(body: BatchScrapeRequest, key: ApiKeyDep) -> dict[str, Any]:
    guard_js_execution(body, key.allow_js_exec)
    # Same gate as a crawl, and it matters more here: the documented maximum is
    # 10,000 URLs in one call.
    billing.assert_credits(key)

    job_id = await repo.create_job(
        "batch",
        key.id,
        body.model_dump(mode="json"),
        webhook_url=body.webhook.url if body.webhook else None,
        webhook_events=list(body.webhook.events) if body.webhook else None,
    )
    await _queue.push(JobMessage(job_id=job_id, kind="batch"), Queue.FETCH_HTTP)
    return {
        "success": True,
        "data": {"id": job_id, "status": str(JobStatus.QUEUED), "total": len(body.urls)},
    }


@router.get("/batch/{job_id}")
async def batch_status(job_id: str, key: ApiKeyDep, request: Request) -> dict[str, Any]:
    job = await _load_job(job_id, key.id)
    warning = await _job_warning(job)
    return {"success": True, "data": _job_payload(job, _base(request), warning)}


@router.get("/batch/{job_id}/pages")
async def batch_pages(
    request: Request,
    job_id: str,
    key: ApiKeyDep,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
) -> dict[str, Any]:
    return await crawl_pages(request, job_id, key, cursor, limit)


# --------------------------------------------------------------------------
# Map — discover URLs without fetching page bodies
# --------------------------------------------------------------------------


@router.post("/map")
async def map_site(body: MapRequest, key: ApiKeyDep, service: ServiceDep) -> dict[str, Any]:
    """Sitemap-first, falling back to a shallow crawl. Fast and cheap."""

    guard_js_execution(body, key.allow_js_exec)
    billing.assert_credits(key)
    await resolve_and_validate(body.url)

    policy = CrawlPolicy(
        root_url=body.url,
        max_depth=1,
        include_subdomains=body.includeSubdomains,
        allow_backward_links=True,
    )
    links: list[dict[str, Any]] = []
    seen: set[str] = set()

    if not body.ignoreSitemap:
        for url in await _sitemap_links(service, body.url, body.limit):
            canonical = normalize_url(url)
            if canonical in seen or policy.evaluate(url, 1):
                continue
            seen.add(canonical)
            links.append({"url": url, "title": None, "source": "sitemap"})
            if len(links) >= body.limit:
                break

    # The platform's own listing: a Shopify store's /products.json names every
    # product, a WordPress site's wp-json every post. Complete, one request a
    # page, and labelled so a caller knows which URLs came from where.
    platform_name: str | None = None
    if len(links) < body.limit:
        platform_name, platform_urls = await _platform_links(service, body.url, body.limit)
        for url in platform_urls:
            canonical = normalize_url(url)
            if canonical in seen or policy.evaluate(url, 1):
                continue
            seen.add(canonical)
            links.append({"url": url, "title": None, "source": "platform"})
            if len(links) >= body.limit:
                break

    # llms.txt is the site's own curated summary for machines, published at a
    # known path on roughly a third of sites measured. One request; listed so an
    # agent mapping a site sees it before anything else.
    llms = await _llms_link(service, body.url)
    if llms and normalize_url(llms) not in seen and len(links) < body.limit:
        seen.add(normalize_url(llms))
        links.insert(0, {"url": llms, "title": "llms.txt", "source": "llms"})

    # Shallow crawl fallback when the sitemap yielded nothing.
    if len(links) < body.limit:
        for url, title in await _shallow_crawl_links(
            service, body.url, policy, plan_concurrency=key.concurrency
        ):
            canonical = normalize_url(url)
            if canonical in seen:
                continue
            seen.add(canonical)
            links.append({"url": url, "title": title, "source": "crawl"})
            if len(links) >= body.limit:
                break

    if body.search:
        needle = body.search.lower()
        links = [
            link
            for link in links
            if needle in link["url"].lower() or needle in (link["title"] or "").lower()
        ]

    # One flat charge per map call, however many URLs came back.
    await billing.charge(key, endpoint="map", url=body.url, cost=Cost(extras={"map": 1}))

    # What crawling what we just found would cost, before anyone commits to it.
    # Map is the cheap half of the pair — it names the URLs without fetching
    # their bodies — so this is the natural place to quote the expensive half.
    # A floor, not a promise: a page forced up to a browser tier costs more.
    found = links[: body.limit]
    per_page = await billing.page_price()

    return {
        "success": True,
        "data": {
            "links": found,
            "platform": platform_name,
            "estimate": {
                "pages": len(found),
                "creditsPerPage": per_page,
                "credits": len(found) * per_page,
                "basis": "direct fetch; a proxied or browser page costs more",
            },
            "cost": {
                "tier": str(Tier.HTTP),
                "tiers_attempted": [str(Tier.HTTP)],
                "proxy_used": False,
                "proxy_type": None,
                "proxy_bytes": 0,
                "browser_ms": 0,
                "extraction_path": None,
                "cached": False,
            },
        },
    }


async def _llms_link(service: Any, root_url: str) -> str | None:
    """`/llms.txt` when it exists and is text, else None. Never a soft-404 page."""
    from engine.core.fetch.base import FetchRequest

    fetcher = service._fetchers.get(Tier.HTTP)
    if fetcher is None:
        return None
    url = f"{root_url.rstrip('/')}/llms.txt"
    try:
        res = await fetcher.fetch(FetchRequest(url=url, timeout_ms=8_000))
    except Exception as exc:  # noqa: BLE001 - most sites have none
        logger.debug("llms_txt_unavailable", url=url, error=str(exc))
        return None
    if res.status_code != 200 or not res.body:
        return None
    head = res.body[:600].lstrip().lower()
    if head.startswith(b"<") or b"<html" in head:
        return None
    return url


async def _platform_links(service: Any, root_url: str, limit: int) -> tuple[str | None, list[str]]:
    """URLs from the platform's own listing endpoint, or ([] , None) on the open core."""
    try:
        from engine.platforms.service import PlatformService
    except ImportError:
        return None, []
    try:
        platforms = PlatformService(service)
        _, _, platform = await platforms.homepage(root_url)
        if platform is None:
            return None, []
        if str(platform) in ("shopify", "woocommerce"):
            listing = await platforms.products(root_url, limit=limit)
            return str(platform), [p.url for p in listing.products if p.url]
        listing = await platforms.posts(root_url, limit=limit)
        return str(platform), [p.url for p in listing.posts if p.url]
    except EngineError:
        return None, []
    except Exception as exc:  # noqa: BLE001 - a listing that fails is not a failed map
        logger.debug("platform_links_failed", url=root_url, error=str(exc))
        return None, []


async def _sitemap_links(service: Any, root_url: str, limit: int) -> list[str]:
    from engine.core.fetch.base import FetchRequest

    fetcher = service._fetchers.get(Tier.HTTP)
    if fetcher is None:
        return []

    candidates: list[str] = []
    # robots.txt is where a site DECLARES its sitemaps, and half of them are not
    # at /sitemap.xml. This read only the cached copy, so the first map of a
    # site — the one that matters — guessed. Fetch it if we have not got it.
    robots = await repo.cached_robots(host_of(root_url))
    if robots is None:
        try:
            res = await fetcher.fetch(
                FetchRequest(url=f"{root_url.rstrip('/')}/robots.txt", timeout_ms=10_000)
            )
            robots = res.text(limit=512_000) if res.status_code == 200 and res.body else ""
        except Exception as exc:  # noqa: BLE001 - no robots.txt is normal
            logger.debug("robots_unavailable", url=root_url, error=str(exc))
            robots = ""
        await repo.store_robots(host_of(root_url), robots)
    if robots:
        candidates.extend(sitemap_urls_from_robots(robots))
    candidates.extend(default_sitemap_urls(root_url))

    found: list[str] = []
    seen: set[str] = set()
    queue = list(candidates)
    fetched = 0

    while queue and len(found) < limit and fetched < 50:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen:
            continue
        seen.add(sitemap_url)
        try:
            result = await fetcher.fetch(FetchRequest(url=sitemap_url, timeout_ms=15_000))
        except Exception as exc:  # noqa: BLE001 - a missing sitemap is normal
            logger.debug("sitemap_unavailable", url=sitemap_url, error=str(exc))
            continue
        fetched += 1
        if result.status_code != 200 or not result.body:
            continue
        pages, nested = parse_sitemap(result.text(limit=10_000_000))
        queue.extend(nested)
        found.extend(pages)

    return found[:limit]


async def _shallow_crawl_links(
    service: Any, root_url: str, policy: CrawlPolicy, plan_concurrency: int | None = None
) -> list[tuple[str, str | None]]:
    from engine.core.errors import EngineError
    from engine.core.frontier.discovery import extract_links

    options = ScrapeOptions(formats=["markdown", "html", "links"], maxAge=3_600_000)
    try:
        outcome = await service.scrape(root_url, options, plan_concurrency=plan_concurrency)
    except EngineError:
        return []

    html = outcome.data.html or ""
    if html:
        discovered = extract_links(html, root_url, policy, depth=0)
        title = title_from_html(html)
        return [(link.url, None) for link in discovered if link.skip_reason is None] + (
            [(root_url, title)] if title else []
        )
    return [(url, None) for url in (outcome.data.links or [])]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def _load_job(job_id: str, api_key_id: str) -> Any:
    job = await repo.get_job(job_id)
    # An unknown id and someone else's id are both 404: confirming that a job
    # exists but belongs to another key leaks information.
    if job is None or job["api_key_id"] != api_key_id:
        raise JobNotFound(job_id)
    return job


def _base(request: Request) -> str:
    """The absolute origin for links we hand back. Firecrawl returns absolute
    `next` URLs and the option names are migration-compatible on purpose; a
    client built against Firecrawl broke on the first page boundary of a
    relative path (measured)."""
    return str(request.base_url).rstrip("/")


NO_WORKER_WARNING = (
    "No worker is consuming this queue. The job will not progress until one is "
    "running: `.venv/bin/python -m engine.workers.http_worker`."
)


def _stale_worker_warning(names: list[str], theirs: str, ours: str) -> str:
    who = ", ".join(names)
    return (
        f"The worker{'s' if len(names) > 1 else ''} consuming this queue "
        f"({who}) {'are' if len(names) > 1 else 'is'} running a different build "
        f"of the engine ({theirs}) from this API ({ours}). Jobs will fail in "
        "ways that look like engine faults until it is restarted."
    )


async def _job_warning(job: Any) -> str | None:
    """What is wrong with the machinery behind this job, if anything.

    Two questions, not one. "Is a worker there" was asked from the day a queued
    job with nothing consuming it was found sitting at `queued` forever
    (relayed 5 Sep 2026). "Is the worker running OUR code" was never asked, and
    on 9 Sep 2026 the answer was no: the daemon had not been restarted since a
    field was added to `Cost`, so every crawl and every batch died on its first
    cached page with an AttributeError, surfaced to the caller as INTERNAL. The
    synchronous half of the API was flawless throughout, which is precisely the
    shape that reads as "the engine is flaky".

    Reported for FINISHED jobs too — the failure it explains is one that has
    already happened.
    """
    from engine.build import build_id
    from engine.workers.queue import stale_workers, worker_builds

    ours = build_id()
    builds = await worker_builds()
    if not builds:
        return NO_WORKER_WARNING if job["status"] in ("queued", "running") else None

    stale = await stale_workers(ours)
    if stale:
        return _stale_worker_warning(stale, builds.get(stale[0], "unknown"), ours)
    return None


def _job_payload(job: Any, base: str, warning: str | None = None) -> dict[str, Any]:
    # Every seeded URL ends in exactly one of completed / failed / skipped, and
    # the three sum to total. `skipped` was always counted in the frontier and
    # never reported — 8 of 54 URLs on a live audit were "missing".
    skipped = job.get("skipped", 0) or 0
    payload: dict[str, Any] = {
        "id": job["id"],
        "status": job["status"],
        "total": job["total"],
        "completed": job["completed"],
        "failed": job["failed"],
        "skipped": skipped,
        # The credits ACTUALLY charged, banked per page by accumulate_job_cost.
        # This was `job["completed"]` — the page count dressed as money. A cache
        # hit is free and a browser rung costs more than tier 0, so the two agree
        # only when every page happened to cost exactly one credit: a 3-page
        # batch reported 3 while the ledger recorded 1 (6 Sep 2026). Operator
        # keys are never metered and correctly report 0.
        "creditsUsed": int((job["cost"] or {}).get("credits", 0) or 0),
        "cost": job["cost"] or {},
        "startedAt": _iso(job["started_at"]),
        "completedAt": _iso(job["completed_at"]),
        "data": [],
    }
    input_payload = job["input"] or {}
    if isinstance(input_payload, dict) and input_payload.get("url"):
        payload["url"] = input_payload["url"]
    kind = job["kind"] if isinstance(job["kind"], str) else str(job["kind"])
    payload["next"] = f"{base}/v1/{kind}/{job['id']}/pages"
    if job["error"]:
        payload["error"] = job["error"]
    if warning:
        payload["warning"] = warning
    return payload


def _requested_options(job: Any) -> ScrapeOptions:
    """The scrapeOptions the caller submitted with the job.

    A crawl and a batch both nest them under `scrapeOptions`; anything
    unparseable falls back to the defaults rather than failing a read of pages
    that were fetched perfectly well.
    """
    payload = job["input"] or {}
    raw = payload.get("scrapeOptions") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return ScrapeOptions()
    try:
        return ScrapeOptions.model_validate(raw)
    except ValidationError:
        return ScrapeOptions()


def _page_payload(row: Any, options: ScrapeOptions | None = None) -> dict[str, Any]:
    # `metadata` is the same object `/v1/scrape` returns, from the same
    # function, so a caller merging the two reads `metadata.sourceURL` on
    # both. The top-level `url`/`sourceURL` stay: clients already read them.
    #
    # The FORMATS come from the same function too. This used to return
    # `markdown` and nothing else, so a crawl or batch that asked for `html`,
    # `rawHtml` or `links` had the work done, stored and billed — and then
    # silently not handed back. A JSON API fetched through a batch returned its
    # body under `markdown` because that was the only field on offer.
    source_url = row["source_url"] or row["url"]
    return {
        "id": row["id"],
        "url": row["url"],
        "sourceURL": source_url,
        "ok": row["ok"],
        "errorCode": row["error_code"],
        # Only what was asked for: a format the caller did not request is
        # ABSENT here, where /v1/scrape returns it present-and-null. The two
        # differ on purpose — a listing carries thousands of rows and a null
        # per unrequested format on each one is pure weight, while a single
        # scrape response is easier to consume with a fixed set of keys. The
        # comment used to claim they matched; they never have.
        **formats_from_page_row(row, options or ScrapeOptions()),
        "metadata": page_metadata_from_row(row, source_url).model_dump(),
    }


def _iso(value: Any) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _encode_cursor(page_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"after": page_id}).encode()).decode()


def _decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, json.JSONDecodeError):
        # A malformed cursor restarts the listing rather than 500ing.
        return None
    after = data.get("after")
    return str(after) if after else None
