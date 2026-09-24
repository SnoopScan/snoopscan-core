"""A breaker for ONE url, so one bad page cannot shut a whole site.

The domain breaker exists to stop us hammering a host that is not answering,
and it is right to have: 542 attempts at a domain with zero lifetime successes
is the case it was built for. But keyed on the domain alone it punishes the
wrong thing. Google's results page never yields, fills the failure window by
itself, and everything else on google.com is refused behind it — measured
20 Sep 2026, when google.com/privacy came back ENGINE_REFUSED because a
SEARCH url had failed earlier. A results page and a privacy policy are not
the same page and must not share a fate.

So when every failure in the window is the SAME url, the domain is left alone
and the url is backed off here instead. Failures spread across several urls
still open the domain breaker, because that is a host-level problem.

Kept in Redis, with the backoff as the TTL: a short-lived refusal that expires
by itself needs no table, no migration and no cleanup job. A deployment with
no Redis simply never backs a url off — the domain breaker still stands, and
the worst case is the behaviour we had before this existed.
"""

from __future__ import annotations

import time

import structlog

logger = structlog.get_logger(__name__)

_PREFIX = "urlbrk"
# A url that keeps failing is not interesting for long. The domain breaker's
# own doubling goes to a day because a dead HOST stays dead; one url is far
# more likely to be a page that changed, so this caps much lower.
MAX_MINUTES = 60


def _key(domain: str, digest: bytes) -> str:
    return f"{_PREFIX}:{domain}:{digest.hex()[:32]}"


async def open_until(domain: str, digest: bytes) -> float | None:
    """Epoch seconds this url stays refused until, or None if it is free.

    Shaped as an epoch so the caller can hand it straight to the existing
    circuit_open_until field and reuse the refusal path already built for the
    domain breaker — one refusal, one error message, one retry hint.
    """
    try:
        from engine.core.politeness import get_redis

        client = await get_redis()
        ttl = await client.ttl(_key(domain, digest))
    except Exception as exc:  # noqa: BLE001 - a breaker outage must not fail a fetch
        logger.debug("url_backoff_unavailable", error=str(exc)[:120])
        return None
    if ttl is None or ttl <= 0:
        return None
    return time.time() + float(ttl)


async def note_failure(domain: str, digest: bytes, minutes: int) -> None:
    """Back this url off for `minutes`, capped."""
    span = max(1, min(minutes, MAX_MINUTES))
    try:
        from engine.core.politeness import get_redis

        client = await get_redis()
        await client.set(_key(domain, digest), "1", ex=span * 60)
    except Exception as exc:  # noqa: BLE001 - see above
        logger.debug("url_backoff_unavailable", error=str(exc)[:120])
        return
    logger.info("url_backed_off", domain=domain, minutes=span)
