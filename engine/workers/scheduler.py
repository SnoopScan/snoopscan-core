"""Recurring maintenance tasks (07-orchestration.md section 9).

A plain interval loop rather than a scheduler library — the spec says not to
build one, and there is nothing here that needs cron semantics.

Every task is idempotent and batched. The frontier reaper is the one that
matters for correctness: without it, a crashed worker's claims sit in
`claimed` for ever and the crawl never terminates.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from engine.core.fetch.escalation import decay_profile, next_tier_down
from engine.core.models import Tier
from engine.logging_config import configure_logging
from engine.settings import settings
from engine.storage import db
from engine.storage import repositories as repo

logger = structlog.get_logger(__name__)


@dataclass
class Task:
    name: str
    interval_s: int
    run: Callable[[], Awaitable[dict[str, int] | int | None]]


async def reap_frontier() -> int:
    """Return stale claims to pending; fail them at the attempt ceiling.

    Runs every minute. It frees a dead worker's URLs — and until 11 Sep 2026
    that was ALL a crash got. The job those URLs belonged to had lost its one
    queue message and nothing re-drove it; see `resume_orphaned_jobs`.
    """
    from engine.workers.queue import CLAIM_TIMEOUT_S

    return await repo.reap_stale_claims(timeout_seconds=CLAIM_TIMEOUT_S)


# Past the claim timeout, so `reap_frontier` has already handed any dead
# worker's claims back and the job has nothing claimed.
RESUME_MARGIN_S = 60
# A job that keeps being orphaned is failed, not retried for ever.
MAX_RESUMES = 5
# Resuming bills new pages. A job idle longer than this is one its caller has
# stopped waiting for, so it is failed rather than quietly charged for days
# after the fact — unless finishing it costs nothing (see below).
RESUME_WINDOW_S = 86_400


def _input(job: Any) -> dict[str, Any]:
    raw = job["input"]
    if isinstance(raw, str):
        import json

        try:
            return dict(json.loads(raw))
        except ValueError:
            return {}
    return dict(raw or {})


def _free_to_finish(job: Any) -> bool:
    """Would resuming this job bill nothing?

    A crawl at its limit, or a job with no URLs left, only needs `_finish` to
    run. Resuming those is always right whatever their age: the alternative is
    failing a crawl that collected every page it was asked for.
    """
    if int(job["pending"]) == 0:
        return True
    limit = _input(job).get("limit")
    return job["kind"] == "crawl" and limit is not None and int(job["done"]) >= int(limit)


async def resume_orphaned_jobs() -> dict[str, int]:
    """Re-drive crawls and batches that nothing is driving.

    A job is carried by ONE Redis message, and `pop` destroys it. A worker that
    stopped or crashed mid-job left it `running` for ever with no message and
    no way back — found 11 Sep 2026, three jobs, the oldest from 1 Sep. Spec 07
    §1 says in-flight jobs are recovered from Postgres, §11 says a crashed job
    is retried, and nothing did either.

    Orphaned means ALL of: nothing claimed, idle past the claim timeout, no
    message waiting, and no live worker saying it holds the job. The last is
    the one that matters — a live worker between two URLs has nothing claimed
    for a moment too, and resuming on that alone would put two workers on one
    crawl.
    """
    from engine.workers import queue as q

    queued = await q.queued_job_ids()
    held = await q.held_job_ids()
    if queued is None or held is None:
        # Cannot see Redis. "Nothing is queued" read off an outage would
        # re-queue every active job at once.
        return {"resumed": 0, "failed": 0, "blind": 1}

    now = datetime.now(UTC)
    resumed = failed = 0

    for job in await repo.orphaned_job_candidates(q.CLAIM_TIMEOUT_S + RESUME_MARGIN_S):
        job_id = job["id"]
        if job_id in queued or job_id in held:
            continue

        idle = (now - job["last_activity"]).total_seconds()
        if not _free_to_finish(job) and (idle > RESUME_WINDOW_S or job["resumes"] >= MAX_RESUMES):
            reason = (
                f"Interrupted {job['resumes']} times without completing."
                if job["resumes"] >= MAX_RESUMES
                else f"Interrupted and idle for {int(idle // 3600)} hours, so not resumed."
            )
            await repo.fail_interrupted_job(
                job_id, f"{reason} Pages already collected are still available."
            )
            logger.warning(
                "job_interrupted_failed", job_id=job_id, resumes=job["resumes"], idle_s=int(idle)
            )
            failed += 1
            continue

        count = await repo.mark_job_resumed(job_id)
        await q.JobQueue().push(
            q.JobMessage(job_id=job_id, kind=str(job["kind"])), q.Queue.FETCH_HTTP
        )
        logger.info("job_resumed", job_id=job_id, resumes=count, idle_s=int(idle))
        resumed += 1

    return {"resumed": resumed, "failed": failed, "blind": 0}


async def sweep_retention() -> dict[str, int]:
    """Batched and LIMITed. An unbounded DELETE on a large table locks it and
    takes the API down with it."""
    return await repo.sweep_expired()


async def decay_domain_profiles() -> int:
    """Lower min_working_tier on domains quiet for 30 days.

    Without decay, a domain that removed its WAF costs browser-tier money for
    ever.
    """
    rows = await db.fetch(
        """
        SELECT domain, min_working_tier,
               EXTRACT(EPOCH FROM (now() - COALESCE(last_block_at, created_at)))
                   / 86400 AS days_quiet
        FROM (
            SELECT domain, min_working_tier, last_block_at, updated_at AS created_at
            FROM domain_profiles
            WHERE min_working_tier <> 'http'
        ) p
        WHERE COALESCE(last_block_at, created_at) < now() - interval '30 days'
        LIMIT 500
        """
    )
    changed = 0
    for row in rows:
        current = Tier(row["min_working_tier"])
        lowered = next_tier_down(current)
        if lowered == current:
            continue
        await db.execute(
            "UPDATE domain_profiles SET min_working_tier = $2 WHERE domain = $1",
            row["domain"],
            str(lowered),
        )
        changed += 1
    if changed:
        logger.info("domain_profiles_decayed", count=changed)
    return changed


async def close_expired_circuits() -> int:
    """Clear breakers whose window has passed, so a domain gets its probe."""
    result = await db.execute(
        """
        UPDATE domain_profiles SET circuit_open_until = NULL
        WHERE circuit_open_until IS NOT NULL AND circuit_open_until < now()
        """
    )
    return int(result.split()[-1]) if result else 0


async def roll_up_proxy_usage() -> int:
    """Aggregate yesterday's proxy spend into `proxy_usage_daily`.

    The budget check sums the day's bytes from `proxy_usage` before every
    proxied fetch. At a few thousand rows that is free; at a few million a day
    it is a growing scan in the hot path of every fetch, and the symptom is the
    whole engine getting slower rather than anything erroring.

    Idempotent on (day, proxy_id, domain), so a re-run after a crash corrects
    the row rather than doubling it. That matters more than it looks: this is
    the table a spend figure would be quoted from.
    """
    result = await db.execute(
        """
        INSERT INTO proxy_usage_daily (day, proxy_id, domain, bytes, requests, successes)
        SELECT date_trunc('day', recorded_at)::date, proxy_id, domain,
               sum(bytes), count(*), count(*) FILTER (WHERE success)
        FROM proxy_usage
        -- 35 days, not 2: a scheduler that misses a run used to lose those days
        -- for good (55.7 MB missing from the month, measured 7 Sep 2026).
        WHERE recorded_at >= date_trunc('day', now()) - interval '35 days'
          AND recorded_at <  date_trunc('day', now())
        GROUP BY 1, 2, 3
        ON CONFLICT (day, proxy_id, domain) DO UPDATE
        SET bytes = EXCLUDED.bytes,
            requests = EXCLUDED.requests,
            successes = EXCLUDED.successes,
            rolled_up_at = now()
        """
    )
    rows = int(result.split()[-1]) if result else 0
    if rows:
        logger.info("proxy_usage_rolled_up", rows=rows)
    return rows


async def check_proxy_health() -> int:
    """Retire proxies that have earned it, on a schedule rather than never.

    `should_retire` and `retire` existed and nothing called them, so a proxy
    that had stopped working stayed in the rotation being chosen and failing.

    Imported inside the function: the proxy layer is proprietary and the open
    core has to run without it. A deployment with no proxy layer does nothing
    here rather than failing to start.
    """
    try:
        from engine.core.proxy import pool
    except ImportError:
        return 0

    candidates = await db.fetch("SELECT id FROM proxies WHERE active = true LIMIT 500")
    retired = 0
    for row in candidates:
        should, reason = await pool.should_retire(row["id"])
        if should:
            await pool.retire(row["id"], reason or "failed health check")
            retired += 1
    if retired:
        logger.warning("proxies_retired", count=retired)
    return retired


# A directory returning this fraction of its previous haul, or less, is treated
# as broken rather than quiet. Directories genuinely shrink, so this is set well
# below normal variation — the alternative is crying wolf on a slow week, and a
# health check nobody believes is not a health check.
DIRECTORY_COLLAPSE_RATIO = 0.5

# Below this many items, a proportional comparison is noise: three items
# becoming one is a 67% fall and means nothing.
DIRECTORY_MIN_ITEMS = 20


async def check_directory_health() -> int:
    """Flag directories that succeeded but stopped returning items.

    This is the silent one. A directory scraper whose target changed its markup
    does not raise — it succeeds and returns twelve items where it used to
    return four hundred. Every success metric stays green, the ingest job
    reports OK, and the data quietly stops arriving.

    So the comparison is against the PREVIOUS count, not against zero. Zero is
    the case that would have been noticed anyway.
    """
    rows = await db.fetch(
        """
        SELECT id, name, last_item_count, previous_item_count, last_ingested_at
        FROM directories
        WHERE active = true AND last_item_count IS NOT NULL
        """
    )

    unhealthy: list[str] = []
    for row in rows:
        current = row["last_item_count"] or 0
        previous = row["previous_item_count"]
        note: str | None = None

        if (
            previous
            and previous >= DIRECTORY_MIN_ITEMS
            and current <= previous * DIRECTORY_COLLAPSE_RATIO
        ):
            note = (
                f"returned {current} items, down from {previous}. "
                f"The target's markup has probably changed — the scraper is "
                f"not failing, it is finding nothing."
            )

        if row["last_ingested_at"] is None:
            note = "has never ingested"

        if note:
            await db.execute(
                """
                UPDATE directories SET last_ingest_ok = false, health_note = $2
                WHERE id = $1
                """,
                row["id"],
                note,
            )
            unhealthy.append(str(row["name"]))
        else:
            await db.execute(
                "UPDATE directories SET last_ingest_ok = true, health_note = NULL WHERE id = $1",
                row["id"],
            )

    # One summary, not one line per directory. 09-leadgen-pipeline.md section 6:
    # "A weekly summary of directory health is worth more than per-run alerting
    # once there are 70 of them." A persistent problem across 70 directories
    # would otherwise emit 70 warnings every run, and a log nobody can read is
    # the same as no log. The per-directory detail is in `health_note`, which is
    # queryable when someone is actually looking.
    if unhealthy:
        logger.warning(
            "directory_health_summary",
            unhealthy_count=len(unhealthy),
            checked=len(rows),
            directories=sorted(unhealthy)[:10],
            truncated=max(0, len(unhealthy) - 10),
        )

    return len(unhealthy)


# A query with an answer that is not going away, in a shape every engine
# handles. The point is to detect a rung refusing us, not to test the web.
SEARCH_CANARY_QUERY = "wikipedia"


async def check_search_health() -> int:
    """Ask every rung a question with a known answer. Returns how many could.

    Search providers do not tell you they have started refusing you; the request
    just comes back empty or 429 and the endpoint quietly gets worse. Measured on
    4 September 2026: Mojeek answered every query at midday and was blocking us
    outright an hour later, and the only reason anyone noticed was a person
    running a benchmark by hand. A canary is the version of that person that
    runs at 3am.

    Logged, not raised. A degraded rung is not an outage — that is the whole
    point of the ladder — but it is the early warning that the ladder is
    thinning, and the last rung failing is worth waking someone for.
    """
    from engine.core import search as serp

    rungs = serp.ladder()
    healthy: list[str] = []
    for provider in rungs:
        try:
            found = await provider.search(serp.SearchQuery(query=SEARCH_CANARY_QUERY, limit=3))
        except Exception as exc:  # noqa: BLE001 - a sick rung must not stop the sweep
            logger.warning("search_rung_unhealthy", provider=provider.name, error=str(exc))
            continue
        if not found:
            logger.warning("search_rung_empty", provider=provider.name)
            continue
        healthy.append(provider.name)

    if not healthy:
        logger.error("search_ladder_down", rungs=[p.name for p in rungs])
    elif len(healthy) < len(rungs):
        logger.warning("search_ladder_thinned", healthy=healthy, rungs=[p.name for p in rungs])
    return len(healthy)


async def run_due_monitors() -> int:
    """Every minute: the monitors whose next_run_at has passed."""
    from engine.api.deps import get_fetchers
    from engine.core.monitor import run_due
    from engine.core.scrape_service import ScrapeService

    return await run_due(ScrapeService(get_fetchers()))


TASKS: list[Task] = [
    Task("frontier_reaper", 60, reap_frontier),
    Task("job_resumer", 60, resume_orphaned_jobs),
    Task("monitor_runner", 60, run_due_monitors),
    Task("circuit_reset", 60, close_expired_circuits),
    Task("retention_sweep", 3_600, sweep_retention),
    Task("proxy_health", 3_600, check_proxy_health),
    # Quarter-hourly: often enough to catch a rung going dark within one
    # support cycle, rare enough that the canary is not itself the load.
    Task("search_health", 900, check_search_health),
    # Daily, not per-run: the spec prefers a periodic summary to alerting on
    # every ingest, and directory health does not change faster than this.
    Task("directory_health", 86_400, check_directory_health),
    # After midnight, so "yesterday" is complete.
    Task("proxy_usage_rollup", 86_400, roll_up_proxy_usage),
    Task("domain_decay", 604_800, decay_domain_profiles),
]


class Scheduler:
    def __init__(self, tasks: list[Task] | None = None) -> None:
        self._tasks = tasks or TASKS
        self._running = False
        self._stopping: asyncio.Event | None = None

    async def run(self) -> None:
        self._running = True
        self._stopping = asyncio.Event()
        self._install_signal_handlers()
        logger.info("scheduler_started", tasks=[t.name for t in self._tasks])

        runners = [asyncio.create_task(self._loop(task)) for task in self._tasks]
        try:
            await asyncio.gather(*runners)
        except asyncio.CancelledError:
            pass
        finally:
            for runner in runners:
                runner.cancel()
            await db.close_pool()
            logger.info("scheduler_stopped")

    async def _loop(self, task: Task) -> None:
        while self._running:
            try:
                result = await task.run()
                if result:
                    logger.info("maintenance_task", task=task.name, result=result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one failing task must not stop the rest
                logger.exception(
                    "maintenance_task_failed",
                    task=task.name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            await self._wait(task.interval_s)

    async def _wait(self, seconds: float) -> None:
        """Sleep between runs, but wake the instant we are asked to stop.

        A bare `asyncio.sleep(interval)` only re-reads `_running` when it
        expires, so SIGTERM was accepted and then ignored for a whole interval
        — and `domain_decay` runs weekly, so the process sat there for up to
        seven days while `pkill` looked like a no-op (seen twice, 6 Sep 2026).
        Under launchd that is worse than untidy: `bootout` waits ~20s and then
        SIGKILLs, so every restart would kill a task mid-write.
        """
        if self._stopping is None:
            await asyncio.sleep(seconds)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    def stop(self) -> None:
        self._running = False
        if self._stopping is not None:
            self._stopping.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop)


async def main() -> None:
    configure_logging(level=settings.log_level)
    await Scheduler().run()


if __name__ == "__main__":
    asyncio.run(main())


__all__ = [
    "Scheduler",
    "TASKS",
    "Task",
    "close_expired_circuits",
    "decay_domain_profiles",
    "decay_profile",
    "reap_frontier",
    "sweep_retention",
]
