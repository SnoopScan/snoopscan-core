"""HTTP worker — consumes crawl and batch jobs (07-orchestration.md section 2).

Async, one process, high concurrency. Memory ~256MB. Scales horizontally.
Browser-tier work belongs in a separate container with a different resource
profile entirely.

Lifecycle:
  * graceful shutdown on SIGTERM — stop claiming, finish in flight, release
    claimed frontier rows, exit
  * a crash is safe: claims are reaped back to `pending` after their timeout
    and re-executed, and every write is idempotent
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from typing import Any

import structlog

from engine.core.frontier.crawler import CrawlRunner
from engine.core.models import BatchScrapeRequest, CrawlRequest, JobStatus
from engine.core.politeness import close_redis, get_redis
from engine.core.scrape_service import ScrapeService
from engine.core.webhooks import WebhookEvent, WebhookSender, send_once
from engine.logging_config import configure_logging
from engine.settings import settings
from engine.storage import db
from engine.storage import repositories as repo
from engine.workers.queue import JobMessage, JobQueue, Queue

logger = structlog.get_logger(__name__)

HEARTBEAT_INTERVAL_S = 15
# The claim timeout and the held-job key live with the queue, which the
# scheduler's reapers read too.
from engine.workers.queue import CLAIM_TIMEOUT_S, WORKER_JOB_PREFIX  # noqa: E402,F401


def worker_name() -> str:
    """Identifies the claim owner in the frontier table."""
    return f"{socket.gethostname()}:{os.getpid()}"


class HttpWorker:
    def __init__(
        self,
        service: ScrapeService,
        queue: JobQueue | None = None,
        *,
        name: str | None = None,
    ) -> None:
        self._service = service
        self._queue = queue or JobQueue()
        # job_id -> plan concurrency, so per-host pacing scales with the plan.
        self._plan_cache: dict[str, int | None] = {}
        self._runner = CrawlRunner(service)
        self._webhooks = WebhookSender()
        self.name = name or worker_name()
        self._running = False
        self._draining = False
        # Which job this worker is driving, published beside the heartbeat so
        # the scheduler can tell an orphan from a job between two URLs.
        self._current_job: str | None = None

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        self._running = True
        self._install_signal_handlers()
        heartbeat = asyncio.create_task(self._heartbeat())
        logger.info("worker_started", worker=self.name)

        try:
            while self._running:
                message = await self._queue.pop(Queue.FETCH_HTTP, block_seconds=5)
                if message is None:
                    continue
                try:
                    await self.handle(message)
                except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
                    logger.exception(
                        "job_failed",
                        job_id=message.job_id,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    # `pop` is destructive: the message is already gone. If the
                    # handler threw before the job reached a terminal or running
                    # state, the job would otherwise sit `queued` forever with
                    # nothing left to run it (seen 6 Sep 2026: a crawl orphaned
                    # at queued because a stale worker popped and dropped it).
                    # Drive it to a terminal state so a caller sees the failure.
                    await self._fail_orphaned(message.job_id, exc)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self._shutdown()

    def stop(self) -> None:
        """Stop claiming new work; in-flight work finishes."""
        logger.info("worker_stopping", worker=self.name)
        self._running = False
        self._draining = True

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop)

    async def _heartbeat(self) -> None:
        """A worker that stops heartbeating has its claims reaped.

        The VALUE is this worker's build id, not a `1`. A worker running older
        code than the API is worse than a worker that is missing: the queue
        drains, every job fails with an internal error, and nothing compares
        the two. That cost an afternoon on 9 Sep 2026 — see engine/build.py.
        """
        from engine.build import build_id

        try:
            client = await get_redis()
            while True:
                await client.set(f"worker:heartbeat:{self.name}", build_id(), ex=60)
                held = getattr(self, "_current_job", None)
                if held:
                    await client.set(f"{WORKER_JOB_PREFIX}{self.name}", held, ex=60)
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - heartbeat loss must not crash the worker
            logger.warning("heartbeat_failed", error=str(exc))

    async def _shutdown(self) -> None:
        await self._webhooks.aclose()
        await db.close_pool()
        await close_redis()
        logger.info("worker_stopped", worker=self.name)

    # -- job handling ------------------------------------------------------

    async def handle(self, message: JobMessage) -> None:
        job = await repo.get_job(message.job_id)
        if job is None:
            logger.warning("job_not_found", job_id=message.job_id)
            return
        if job["status"] in ("cancelled", "completed", "failed"):
            return

        await self._hold(job["id"])
        try:
            if job["kind"] == "crawl":
                await self._run_crawl(job)
            elif job["kind"] == "batch":
                await self._run_batch(job)
            elif job["kind"] == "leads":
                await self._run_leads(job)
            else:
                logger.warning("unsupported_job_kind", kind=job["kind"], job_id=job["id"])
        finally:
            await self._unhold()

    async def _hold(self, job_id: str) -> None:
        """Say which job this worker is driving — at once, not at the next
        heartbeat, so there is no window in which it looks orphaned."""
        self._current_job = job_id
        try:
            client = await get_redis()
            await client.set(f"{WORKER_JOB_PREFIX}{self.name}", job_id, ex=60)
        except Exception as exc:  # noqa: BLE001 - the idle bound still protects the job
            logger.warning("job_hold_failed", job_id=job_id, error=str(exc))

    async def _unhold(self) -> None:
        self._current_job = None
        try:
            client = await get_redis()
            await client.delete(f"{WORKER_JOB_PREFIX}{self.name}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("job_unhold_failed", error=str(exc))

    async def _hand_back(self, job: Any, processed: int) -> None:
        """Stop mid-job without abandoning it.

        Spec 07 §2: on shutdown, release claimed frontier rows. They were left
        to the five-minute reaper instead, and the job itself was simply
        dropped: its one queue message had been popped, nothing re-pushed it,
        and it stayed `running` for ever. The runbook restarts the worker after
        every pull, so this happened on every deploy that caught a crawl.

        Now the claims go back at once and the job goes back on the queue for
        the next worker — the restarted one, usually. If Redis is down at that
        moment the scheduler's job reaper is the backstop.
        """
        job_id = job["id"]
        try:
            released = await repo.release_claims(job_id, self.name)
            await self._queue.push(
                JobMessage(job_id=job_id, kind=str(job["kind"])), Queue.FETCH_HTTP
            )
            logger.info("job_handed_back", job_id=job_id, processed=processed, released=released)
        except Exception as exc:  # noqa: BLE001 - the reaper recovers what this cannot
            logger.warning("job_hand_back_failed", job_id=job_id, error=str(exc))

    async def _fail_orphaned(self, job_id: str, exc: Exception) -> None:
        """Mark a non-terminal job failed after its handler raised.

        Only touches a job still `queued` or `running`: a job that reached its
        own terminal state, or was cancelled, is left exactly as it is.
        """
        try:
            job = await repo.get_job(job_id)
            if job is None or job["status"] not in ("queued", "running"):
                return
            await repo.set_job_status(
                job_id,
                JobStatus.FAILED,
                error={"code": "INTERNAL", "message": f"{type(exc).__name__}: {str(exc)[:300]}"},
            )
        except Exception as inner:  # noqa: BLE001 - failing to fail must not kill the worker
            logger.error("orphan_fail_failed", job_id=job_id, error=str(inner))

    async def _run_crawl(self, job: Any) -> None:
        job_id = job["id"]
        request = CrawlRequest.model_validate(job["input"])

        await repo.set_job_status(job_id, JobStatus.RUNNING)
        await self._notify(job, "started", {"url": request.url})

        if await repo.frontier_row_count(job_id) == 0:
            seeded = await self._runner.seed(job_id, request)
            logger.info(
                "crawl_seeded",
                job_id=job_id,
                seeded=seeded.seeded,
                skipped=seeded.skipped,
                source=seeded.source,
            )

        processed = 0
        while not self._draining:
            # A cancellation lands in the jobs table, so check it each turn
            # rather than only at the start.
            current = await repo.get_job(job_id)
            if current is None or current["status"] == "cancelled":
                logger.info("crawl_cancelled", job_id=job_id)
                return

            advanced = await self._runner.process_one(job_id, request, self.name)
            if not advanced:
                break
            processed += 1
            await self._notify_page(job, job_id)

        if self._draining:
            await self._hand_back(job, processed)
            return

        await self._finish(job, job_id, processed)

    async def _run_batch(self, job: Any) -> None:
        job_id = job["id"]
        request = BatchScrapeRequest.model_validate(job["input"])

        await repo.set_job_status(job_id, JobStatus.RUNNING)
        await self._notify(job, "started", {"urls": len(request.urls)})

        if await repo.frontier_row_count(job_id) == 0:
            from engine.core.urls import normalized_hash, url_hash

            rows = [
                {
                    "url": url,
                    "url_hash": url_hash(url),
                    "normalized_hash": normalized_hash(url),
                    "depth": 0,
                    "discovered_via": "batch",
                }
                for url in request.urls
            ]
            await repo.add_frontier_urls(job_id, rows)
            await repo.refresh_job_counters(job_id)

        semaphore = asyncio.Semaphore(request.maxConcurrency)
        processed = 0

        while not self._draining:
            row = await repo.claim_frontier_url(job_id, self.name)
            if row is None:
                break
            async with semaphore:
                await self._scrape_batch_url(job_id, row, request)
            processed += 1
            await repo.refresh_job_counters(job_id)

        if self._draining:
            await self._hand_back(job, processed)
            return

        await self._finish(job, job_id, processed)

    def _leads_service(self) -> Any:
        """Find Leads needs the browser rungs; this worker's own ladder has only
        tiers 0/1 unless ENGINE_WORKER_TIERS says otherwise, so crawls and
        batches stay cheap. Measured on the first live run: a Maps search asked
        for `browser` and `stealth` went out as `impersonate` both times, and
        came back near-empty. Leads get the API's full ladder, built once."""
        service = getattr(self, "_full_ladder", None)
        if service is None:
            from engine.api.deps import get_fetchers

            service = ScrapeService(get_fetchers())
            self._full_ladder = service
        return service

    async def _run_leads(self, job: Any) -> None:
        """Find Leads. Proprietary: the open core has no engine.leads, and a
        leads job there fails with a reason rather than sitting queued."""
        import time

        from engine.core.models import Cost

        job_id = job["id"]
        try:
            from engine.leads.models import LeadsRequest
            from engine.leads.service import LeadsService, charges
        except ImportError:
            await repo.set_job_status(
                job_id,
                JobStatus.FAILED,
                {"code": "LEADS_UNAVAILABLE", "message": "Find Leads is not available here."},
            )
            return

        request = LeadsRequest.model_validate(job["input"])
        await repo.set_job_status(job_id, JobStatus.RUNNING)
        await self._notify(job, "started", {"limit": request.limit})

        last_stage = ""
        last_at = 0.0

        async def progress(stage: str, done: int, total: int) -> None:
            # Written on a change of stage, at the end, and at most every two
            # seconds in between: a 250-lead run is not 250 database writes.
            nonlocal last_stage, last_at
            now = time.monotonic()
            if stage != last_stage or done >= total or now - last_at >= 2:
                last_stage, last_at = stage, now
                await repo.set_job_progress(job_id, stage, total, done)

        run = await LeadsService(self._leads_service()).run(request, progress)
        await repo.store_lead_results(job_id, [lead.model_dump(mode="json") for lead in run.leads])

        # Charged once, per lead delivered. A job resumed after a restart runs
        # again from the start, and must not charge a second time.
        fresh = await repo.get_job(job_id)
        already = bool(((fresh["cost"] if fresh else None) or {}).get("charged"))
        extras = charges(run)
        credits = 0
        if not already and sum(extras.values()) > 0:
            key = await repo.job_api_key(job_id)
            if key is not None:
                from engine.api import billing

                cost = Cost(extras=extras)
                await billing.charge(key, endpoint="leads", url=None, cost=cost, job_id=job_id)
                credits = cost.credits or 0
        if not already:
            await repo.merge_job_cost(
                job_id,
                {
                    "charged": True,
                    "credits": credits,
                    "charges": extras,
                    "listings": run.listings,
                    "merged": run.merged,
                    "sources": {
                        name: {"found": r.found, "error": r.error}
                        for name, r in run.sources.items()
                    },
                },
            )
        await repo.set_job_progress(job_id, "done", len(run.leads), len(run.leads))
        await repo.set_job_status(job_id, JobStatus.COMPLETED)
        logger.info("leads_completed", job_id=job_id, leads=len(run.leads), credits=credits)
        await self._notify(job, "completed", {"leads": len(run.leads)})

    async def _scrape_batch_url(self, job_id: str, row: Any, request: Any) -> None:
        """A per-URL failure does not fail the batch — the status appears in
        the pages response instead."""
        from engine.core.errors import EngineError
        from engine.core.urls import normalized_hash

        try:
            outcome = await self._service.scrape(
                row["url"],
                request.scrapeOptions,
                job_id=job_id,
                worker=self.name,
                plan_concurrency=await self._plan_concurrency(job_id),
            )
        except EngineError as exc:
            await repo.store_page(
                {
                    "job_id": job_id,
                    "url": row["url"],
                    "source_url": row["url"],
                    "normalized_hash": normalized_hash(row["url"]),
                    "ok": False,
                    "error_code": str(exc.code),
                }
            )
            await repo.complete_frontier_url(row["id"], ok=False)
            return
        await repo.complete_frontier_url(row["id"], ok=True)
        # Everything below was missing entirely: `_scrape_batch_url` threw the
        # outcome away, so a batch recorded no cost, wrote no usage event and
        # charged nothing — 4 pages delivered to a real customer key produced
        # 0 ledger rows, while `/v1/batch/{id}` reported `creditsUsed` to that
        # same customer (proved on live data, 6 Sep 2026). The crawl path did
        # all three; batch is the same unit of work and now does too.
        cost = outcome.data.cost
        # Meter BEFORE accumulating: billing.charge is what fills cost.credits,
        # so the other order banks a zero and `creditsUsed` under-reports.
        await self._meter_batch(job_id, row["url"], cost)
        await repo.accumulate_job_cost(
            job_id,
            cost.proxy_bytes,
            cost.browser_ms,
            cost.tier or "unknown",
            cost.credits or 0,
        )
        logger.info(
            "batch_page_done",
            job_id=job_id,
            url=row["url"],
            tier=cost.tier,
            cached=outcome.from_cache,
            words=outcome.data.metadata.wordCount,
        )

    async def _plan_concurrency(self, job_id: str) -> int | None:
        """The plan behind this job, looked up once per job and remembered."""
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
        if len(cache) > 512:
            cache.clear()
        cache[job_id] = plan
        return plan

    async def _meter_batch(self, job_id: str, url: str, cost: Any) -> None:
        """Charge the job's key for one batched page; operator keys are never
        metered (billing.charge returns early on a null owner_ref)."""
        from engine.api import billing

        key = await repo.job_api_key(job_id)
        if key is not None:
            await billing.charge(key, endpoint="batch", url=url, cost=cost, job_id=job_id)

    async def _finish(self, job: Any, job_id: str, processed: int) -> None:
        # A crawl that stopped at its limit leaves URLs it never fetched; land
        # them in `skipped` so completed + failed + skipped == total and the
        # run reconciles (measured). A crawl that emptied its frontier
        # has none to move — a no-op there.
        skipped = await repo.skip_pending_frontier(job_id, "limit_reached")
        if skipped:
            logger.info("crawl_limit_skipped", job_id=job_id, skipped=skipped)
        await repo.refresh_job_counters(job_id)
        await repo.set_job_status(job_id, JobStatus.COMPLETED)
        final = await repo.get_job(job_id)
        logger.info("job_completed", job_id=job_id, processed=processed)
        await self._notify(
            job,
            "completed",
            {
                "total": final["total"] if final else 0,
                "completed": final["completed"] if final else 0,
                "failed": final["failed"] if final else 0,
            },
        )

    # -- webhooks ----------------------------------------------------------

    async def _notify(self, job: Any, event: str, data: dict[str, Any]) -> None:
        url = job["webhook_url"]
        events = job["webhook_events"] or []
        if not url or event not in events:
            return
        key = await self._webhook_secret(job["api_key_id"])
        redis = await get_redis()
        await send_once(
            self._webhooks,
            redis,
            url,
            WebhookEvent(event=event, job_id=job["id"], data=data),
            secret=key,
        )

    async def _notify_page(self, job: Any, job_id: str) -> None:
        events = job["webhook_events"] or []
        if not job["webhook_url"] or "page" not in events:
            return
        pages = await repo.list_job_pages(job_id, limit=1)
        if not pages:
            return
        page = pages[-1]
        key = await self._webhook_secret(job["api_key_id"])
        redis = await get_redis()
        await send_once(
            self._webhooks,
            redis,
            job["webhook_url"],
            WebhookEvent(
                event="page",
                job_id=job_id,
                page_id=page["id"],
                data={"url": page["url"], "title": page["title"], "ok": page["ok"]},
            ),
            secret=key,
        )

    async def _webhook_secret(self, api_key_id: str) -> str | None:
        row = await db.fetchrow("SELECT webhook_secret FROM api_keys WHERE id = $1", api_key_id)
        return row["webhook_secret"] if row else None


def _worker_fetchers() -> dict[Any, Any]:
    """The rungs this worker may climb: `ENGINE_WORKER_TIERS`, default tiers 0/1.

    Reuses the API's own ladder so a deployment that wired a browser gets the
    same one here. Anything named but not wired is simply absent; an empty
    result falls back to tiers 0/1 rather than a worker that can fetch nothing.
    """
    from engine.api.deps import get_fetchers
    from engine.core.fetch.tier0_http import HttpFetcher
    from engine.core.fetch.tier1_impersonate import ImpersonateFetcher
    from engine.core.models import Tier

    wanted = {t.strip().lower() for t in settings.worker_tiers.split(",") if t.strip()}
    chosen = {tier: f for tier, f in get_fetchers().items() if str(tier) in wanted}
    if not chosen:
        chosen = {Tier.HTTP: HttpFetcher(), Tier.IMPERSONATE: ImpersonateFetcher()}
    logger.info("worker_tiers", tiers=[str(t) for t in chosen])
    return chosen


async def main() -> None:
    configure_logging(level=settings.log_level)
    service = ScrapeService(_worker_fetchers())
    await HttpWorker(service).run()


if __name__ == "__main__":
    asyncio.run(main())
