"""Queue definitions (07-orchestration.md section 1).

Redis + RQ. Not Celery — its configuration surface and operational weight are
unjustified at this scale.

Two things matter here:

  * `fetch:http` and `fetch:browser` are separate queues consumed by separate
    worker pools. Browser jobs are memory-bound and slow, HTTP jobs are
    network-bound and fast; one pool for both means browser work starves HTTP
    throughput, or HTTP concurrency exhausts RAM.
  * Payloads carry IDS, never data. A queue message is {job_id, frontier_id}
    and the worker loads what it needs from Postgres, so Redis stays small and
    a payload cannot go stale.

Redis holds queue state ONLY. If it is lost, in-flight work is recovered from
Postgres: frontier rows stuck in `claimed` past their timeout return to
`pending`. Redis is a cache, not a system of record.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import redis.asyncio as aioredis
import structlog

from engine.core.politeness import MAX_BLOCKING_SECONDS, get_redis

logger = structlog.get_logger(__name__)


class Queue(StrEnum):
    FETCH_HTTP = "fetch:http"
    FETCH_BROWSER = "fetch:browser"
    EXTRACT = "extract"
    LEADGEN = "leadgen"
    MAINTENANCE = "maintenance"


class Priority(StrEnum):
    """Consumed in order. A synchronous /v1/scrape must not queue behind a
    10,000-page crawl."""

    HIGH = "high"
    DEFAULT = "default"
    LOW = "low"


@dataclass(frozen=True)
class JobMessage:
    """Ids only — never page data."""

    job_id: str
    frontier_id: int | None = None
    kind: str = "crawl"

    def encode(self) -> str:
        import json

        return json.dumps(
            {"job_id": self.job_id, "frontier_id": self.frontier_id, "kind": self.kind}
        )

    @classmethod
    def decode(cls, raw: str | bytes) -> JobMessage:
        import json

        data: dict[str, Any] = json.loads(raw)
        return cls(
            job_id=str(data["job_id"]),
            frontier_id=data.get("frontier_id"),
            kind=str(data.get("kind", "crawl")),
        )


# How long a frontier claim may sit before the reaper hands it back. One
# number, read by the worker and by both scheduler reapers. It had been a
# constant in the worker and a bare 300 in `reap_stale_claims`' signature —
# two copies of the same promise.
CLAIM_TIMEOUT_S = 300

# Which job each live worker is driving right now. The heartbeat says a worker
# exists; this says what it is doing — which is the only way to tell an
# orphaned job from one whose worker is simply between two URLs.
WORKER_JOB_PREFIX = "worker:job:"


def queue_key(queue: Queue, priority: Priority = Priority.DEFAULT) -> str:
    return f"queue:{priority}:{queue}"


class JobQueue:
    """A thin list-backed queue.

    RQ is the spec's choice for running Python functions on a worker; the
    crawl loop needs only push/pop of id payloads, so this wraps Redis lists
    directly rather than paying RQ's serialisation for a two-field message.
    """

    def __init__(self, redis: aioredis.Redis | None = None) -> None:
        self._redis = redis

    async def _client(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await get_redis()
        return self._redis

    async def push(
        self,
        message: JobMessage,
        queue: Queue = Queue.FETCH_HTTP,
        priority: Priority = Priority.DEFAULT,
    ) -> None:
        client = await self._client()
        await client.lpush(queue_key(queue, priority), message.encode())

    async def pop(
        self, queue: Queue = Queue.FETCH_HTTP, block_seconds: int = 5
    ) -> JobMessage | None:
        """Blocking pop across priorities, highest first.

        `block_seconds` is Redis's own BRPOP block time, not a cancellation
        deadline for this coroutine.
        """
        if block_seconds > MAX_BLOCKING_SECONDS:
            raise ValueError(
                f"block_seconds={block_seconds} exceeds MAX_BLOCKING_SECONDS; "
                "the Redis read timeout would abort the pop"
            )
        client = await self._client()
        keys = [queue_key(queue, p) for p in (Priority.HIGH, Priority.DEFAULT, Priority.LOW)]
        result = await client.brpop(keys, timeout=block_seconds)
        if result is None:
            return None
        _, raw = result
        return JobMessage.decode(raw)

    async def depth(
        self, queue: Queue = Queue.FETCH_HTTP, priority: Priority = Priority.DEFAULT
    ) -> int:
        client = await self._client()
        return int(await client.llen(queue_key(queue, priority)))

    async def clear(self, queue: Queue) -> None:
        client = await self._client()
        for priority in Priority:
            await client.delete(queue_key(queue, priority))


# --------------------------------------------------------------------------
# Is anything on the other end of the queue?
# --------------------------------------------------------------------------

WORKER_HEARTBEAT_PREFIX = "worker:heartbeat:"


async def live_workers() -> list[str]:
    """Names of workers that have heartbeated inside the TTL.

    A job queued with nothing consuming it sits at `queued` forever and looks
    exactly like a job that is about to start. The heartbeat was already being
    written for claim-reaping; nothing read it (relayed 5 Sep 2026: "batch jobs
    need the worker running"). Returns [] on any Redis trouble — a readiness
    signal must never be the thing that breaks the request it decorates.
    """
    try:
        client = await get_redis()
        keys = [k async for k in client.scan_iter(match=f"{WORKER_HEARTBEAT_PREFIX}*", count=100)]
    except Exception as exc:  # noqa: BLE001 - never fail a request over this
        logger.debug("worker_heartbeat_scan_failed", error=str(exc))
        return []
    return sorted(k.removeprefix(WORKER_HEARTBEAT_PREFIX) for k in keys)


async def worker_builds() -> dict[str, str]:
    """Each live worker's name against the build id it is running.

    Same read as `live_workers`, one level richer, because "a worker is there"
    and "the worker is running our code" are different questions and only the
    first was ever asked.
    """
    try:
        client = await get_redis()
        keys = [k async for k in client.scan_iter(match=f"{WORKER_HEARTBEAT_PREFIX}*", count=100)]
        if not keys:
            return {}
        values = await client.mget(keys)
    except Exception as exc:  # noqa: BLE001 - never fail a request over this
        logger.debug("worker_heartbeat_scan_failed", error=str(exc))
        return {}

    out: dict[str, str] = {}
    for key, value in zip(keys, values, strict=False):
        name = key.removeprefix(WORKER_HEARTBEAT_PREFIX)
        # Workers before this change wrote a literal "1". Unknown, not stale:
        # accusing an old heartbeat of a mismatch it cannot answer would be a
        # false alarm on every rolling deploy.
        out[name] = "" if value in (None, "1") else str(value)
    return out


async def stale_workers(current: str) -> list[str]:
    """Live workers whose build is not `current`. Blank builds do not count."""
    return sorted(
        name for name, build in (await worker_builds()).items() if build and build != current
    )


async def queued_job_ids(queue: Queue = Queue.FETCH_HTTP) -> set[str] | None:
    """Every job id with a message waiting, across all priorities.

    None, not an empty set, when Redis cannot be read. The difference matters:
    an empty set means "nothing is queued, resume what is orphaned", and
    reading that off an outage would re-queue every active job at once.
    """
    try:
        client = await get_redis()
        found: set[str] = set()
        for priority in (Priority.HIGH, Priority.DEFAULT, Priority.LOW):
            for raw in await client.lrange(queue_key(queue, priority), 0, -1):
                try:
                    found.add(JobMessage.decode(raw).job_id)
                except (ValueError, KeyError):
                    continue
        return found
    except Exception as exc:  # noqa: BLE001 - an unreadable queue means "do nothing"
        logger.warning("queue_scan_failed", error=str(exc))
        return None


async def held_job_ids() -> set[str] | None:
    """Job ids a live worker says it is driving. None when Redis is unreadable."""
    try:
        client = await get_redis()
        keys = [k async for k in client.scan_iter(match=f"{WORKER_JOB_PREFIX}*", count=100)]
        if not keys:
            return set()
        return {str(v) for v in await client.mget(keys) if v}
    except Exception as exc:  # noqa: BLE001
        logger.warning("held_jobs_scan_failed", error=str(exc))
        return None
