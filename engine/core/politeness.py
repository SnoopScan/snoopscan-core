"""Politeness — per-domain delay and concurrency (03-fetch-tiers.md s8).

Enforced through Redis so limits hold across ALL workers. Local-only rate
limiting fails the moment there is more than one worker, which is immediately.

Politeness is not optional and not configurable below the floor. Hammering a
target burns our IP ranges and is the fastest route to a complaint.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import redis.asyncio as aioredis
from redis.commands.core import AsyncScript

from engine.settings import settings

_redis: aioredis.Redis | None = None
# The loop that made `_redis`. A client is usable, and closable, only there.
_redis_loop: asyncio.AbstractEventLoop | None = None

# Atomic TICKET: the caller claims the next free instant for this domain and is
# told exactly how long to sleep before taking it.
#
# The previous version answered "not yet, try again in N" — so every contender
# was handed the same N, woke together, raced for one slot, and the losers paid
# another full round. Measured: 26 pages against one host took 25.7s when the
# pacing allowed 2.6s, and at ten seconds of patience nearly half were dropped
# as failures.
#
# A ticket cannot collide. Each caller advances the counter by exactly one
# delay, so the queue is first-come-first-served and throughput is precisely
# 1/delay however many callers there are. A caller whose turn would fall beyond
# what it can wait for takes no ticket at all, leaving the queue untouched for
# everyone behind it.
_DELAY_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local delay = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local max_wait = tonumber(ARGV[4])
local next_allowed = tonumber(redis.call('GET', key) or 0)
local start = now
if next_allowed > now then
    start = next_allowed
end
local wait = start - now
if wait > max_wait then
    return -1
end
redis.call('SET', key, start + delay, 'PX', ttl)
return wait
"""

# Concurrency slot acquisition, capped per domain.
_SLOT_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local current = tonumber(redis.call('GET', key) or 0)
if current < limit then
    redis.call('INCR', key)
    redis.call('PEXPIRE', key, ttl)
    return 1
end
return 0
"""


# redis-py 5+ ships a DEFAULT socket read timeout of 5 s. A worker's blocking
# pop (BRPOP, block 5 s) then times out on the read the moment the queue is
# idle, and the worker dies. The read timeout must outlast any block time.
REDIS_SOCKET_TIMEOUT = 30.0
REDIS_CONNECT_TIMEOUT = 5.0
MAX_BLOCKING_SECONDS = 10  # the longest BRPOP any worker may ask for


async def get_redis() -> aioredis.Redis:
    """The process's client, made on the running loop.

    One loop in production, so one client for life. A client made on another
    loop cannot be used here and is replaced rather than handed on: across a
    test session it was inherited by the next test and failed there, in
    whichever test happened to run next.
    """
    global _redis, _redis_loop
    loop = asyncio.get_running_loop()
    if _redis is not None and _redis_loop is not loop:
        _redis = None
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=settings.redis_max_connections,
            socket_timeout=REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT,
        )
        _redis_loop = loop
    return _redis


async def close_redis() -> None:
    """Close the client if this loop made it; otherwise just let it go.

    Closing a client from a loop other than its own raises "Event loop is
    closed" — the app's shutdown did exactly that to a client a previous test
    had made, and failed a test that had done nothing wrong.
    """
    global _redis, _redis_loop
    client, made_on = _redis, _redis_loop
    _redis, _redis_loop = None, None
    if client is not None and made_on is asyncio.get_running_loop():
        await client.aclose()


@dataclass(frozen=True)
class HostBudget:
    """What one caller may do to one host: how fast, and how many at once."""

    delay_ms: int
    concurrency: int


def budget_for_plan(plan_concurrency: int | None) -> HostBudget:
    """Per-host pacing derived from the plan the caller bought.

    Pure and side-effect free so the curve can be asserted directly rather than
    inferred from Redis behaviour. A caller with no plan attached gets the free
    plan's pacing — the safe end, never the fast one.
    """
    ref = max(1, settings.politeness_reference_concurrency)
    if not plan_concurrency or plan_concurrency <= 0:
        return HostBudget(
            settings.politeness_default_delay_ms, settings.politeness_default_concurrency
        )

    # Delay falls as the plan grows, from the free plan's spacing down to the
    # floor. round() not int(): truncation quietly makes everyone faster.
    delay = round(settings.politeness_default_delay_ms * ref / plan_concurrency)
    delay = max(
        settings.politeness_floor_delay_ms, min(delay, settings.politeness_default_delay_ms)
    )

    # One host may take a share of the plan's budget, never all of it: a single
    # site should not be able to consume every slot the customer paid for.
    concurrency = max(
        settings.politeness_default_concurrency,
        round(plan_concurrency / max(1, settings.politeness_host_share)),
    )
    concurrency = min(concurrency, settings.politeness_max_host_concurrency)

    return HostBudget(delay, concurrency)


@dataclass
class PolitenessDecision:
    allowed: bool
    # When allowed, this is the ticket: sleep it, then go. When not allowed it
    # is how long to wait before asking again, or None if asking again is
    # pointless right now.
    wait_ms: int | None = 0
    reason: str | None = None


class PolitenessGate:
    """Token bucket keyed on domain, shared across every worker."""

    def __init__(self, redis: aioredis.Redis | None = None) -> None:
        self._redis = redis
        # Script objects, not hashes we loaded once: a Redis restart wipes its
        # script cache, and a saved hash then fails every request with
        # NoScriptError until the API restarts. Unattended upgrades restarted
        # Redis on 25 Sep 2026 and every scrape failed from the first call
        # after. A registered script re-sends itself when Redis has lost it.
        self._delay: AsyncScript | None = None
        self._slot: AsyncScript | None = None

    async def _client(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await get_redis()
        return self._redis

    async def _scripts(self) -> tuple[AsyncScript, AsyncScript]:
        client = await self._client()
        if self._slot is None:
            self._slot = client.register_script(_SLOT_SCRIPT)
        if self._delay is None:
            self._delay = client.register_script(_DELAY_SCRIPT)
        return self._slot, self._delay

    async def acquire(
        self,
        domain: str,
        *,
        delay_ms: int | None = None,
        max_concurrency: int | None = None,
        floor_delay_ms: int | None = None,
        max_wait_ms: int = 30_000,
    ) -> PolitenessDecision:
        """Claim this domain's next free instant.

        Returns allowed=True with `wait_ms` — the caller sleeps that and goes.
        Returns allowed=False when the queue is longer than `max_wait_ms`, or
        when the domain is already at its concurrency limit; in neither case
        has a ticket been taken.
        """
        client = await self._client()
        slot_script, delay_script = await self._scripts()

        # The floor is the caller's plan, and a domain may only ever RAISE the
        # delay above it — a site that answered 429 stays slowed for everyone,
        # whatever they pay.
        floor = (
            floor_delay_ms if floor_delay_ms is not None else settings.politeness_default_delay_ms
        )
        delay = max(delay_ms or 0, floor)
        limit = max(1, max_concurrency or settings.politeness_default_concurrency)

        slot_key = f"politeness:slots:{domain}"
        got_slot = await slot_script(keys=[slot_key], args=[limit, 120_000])
        if not int(got_slot):
            return PolitenessDecision(False, wait_ms=250, reason="max_concurrency")

        delay_key = f"politeness:next:{domain}"
        now_ms = int(time.time() * 1000)
        wait = int(
            await delay_script(
                keys=[delay_key], args=[now_ms, delay, max(delay * 8, 60_000), max_wait_ms]
            )
        )
        if wait < 0:
            # The queue for this domain is longer than the caller can wait for.
            # No ticket was taken, so nobody behind them is delayed by this.
            await client.decr(slot_key)
            return PolitenessDecision(False, wait_ms=None, reason="queue_too_long")

        # A ticket, not a refusal: sleep this and the instant is theirs. The
        # slot stays held — this request IS running, it just has not started.
        return PolitenessDecision(True, wait_ms=wait)

    async def release(self, domain: str) -> None:
        client = await self._client()
        key = f"politeness:slots:{domain}"
        # Never let the counter go negative: a crashed worker's slot is
        # reclaimed by the key's TTL, and a double-release must not free
        # capacity that was never taken.
        await client.eval(
            "local n = tonumber(redis.call('GET', KEYS[1]) or 0) "
            "if n > 0 then return redis.call('DECR', KEYS[1]) end return 0",
            1,
            key,
        )

    async def note_retry_after(self, domain: str, seconds: int) -> None:
        """A 429's Retry-After is obeyed exactly."""
        client = await self._client()
        next_allowed = int(time.time() * 1000) + seconds * 1000
        await client.set(f"politeness:next:{domain}", next_allowed, px=max(seconds * 1000, 60_000))


# How long a day's count of rate-limited requests is kept. The app reads
# today's; a few days' grace lets it catch up after downtime.
REFUSED_TTL_S = 3 * 86_400


def refused_key(day: str) -> str:
    """One Redis hash per UTC day: api key id -> requests refused with a 429."""
    return f"ratelimited:{day}"


class RateLimiter:
    """Per-API-key request limiting. Exists to stop a runaway loop, not to
    monetise.

    Every refusal is also counted, per key per UTC day, so the operator app
    can tell a customer their key keeps hitting its limit — a 429 their code
    swallows looks like missing data, not an error. Read with `refused_on`,
    served as GET /internal/rate-limited. One HINCRBY per refused request,
    nothing on an allowed one.
    """

    def __init__(self, redis: aioredis.Redis | None = None) -> None:
        self._redis = redis

    async def _client(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await get_redis()
        return self._redis

    async def check(self, key_id: str, limit_rpm: int) -> tuple[bool, int, int]:
        """Returns (allowed, remaining, reset_epoch_seconds)."""
        client = await self._client()
        window = int(time.time() // 60)
        redis_key = f"ratelimit:{key_id}:{window}"
        count = await client.incr(redis_key)
        if count == 1:
            await client.expire(redis_key, 120)
        remaining = max(0, limit_rpm - count)
        reset = (window + 1) * 60
        allowed = count <= limit_rpm
        if not allowed:
            await self._count_refusal(client, key_id)
        return allowed, remaining, reset

    async def _count_refusal(self, client: aioredis.Redis, key_id: str) -> None:
        # Best effort: a failure to COUNT a 429 must never turn it into a 500.
        day = time.strftime("%Y-%m-%d", time.gmtime())
        try:
            await client.hincrby(refused_key(day), key_id, 1)
            await client.expire(refused_key(day), REFUSED_TTL_S)
        except Exception:  # noqa: BLE001
            return

    async def refused_on(self, day: str) -> list[dict[str, str | int]]:
        """Keys refused on one UTC day (YYYY-MM-DD), busiest first."""
        client = await self._client()
        raw = await client.hgetall(refused_key(day))
        rows: list[dict[str, str | int]] = [
            {"key_id": str(k), "refused": int(v)} for k, v in (raw or {}).items()
        ]
        rows.sort(key=lambda r: int(r["refused"]), reverse=True)
        return rows
