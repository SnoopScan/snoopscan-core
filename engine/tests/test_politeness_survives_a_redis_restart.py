"""A Redis restart must not take scraping down.

25 Sep 2026: unattended upgrades restarted Redis, which empties its script
cache. The API had loaded its politeness scripts once and kept only their
hashes, so from the first request afterwards every scrape failed with
NoScriptError until the API itself was restarted. SCRIPT FLUSH is exactly
what a restart does to the cache.
"""

from __future__ import annotations

import uuid

import pytest
import redis.asyncio as aioredis

from engine.core.politeness import PolitenessGate
from engine.settings import settings


@pytest.mark.asyncio
async def test_the_gate_keeps_working_after_redis_loses_its_scripts() -> None:
    client = aioredis.from_url(settings.redis_url)
    try:
        await client.ping()
    except Exception:
        pytest.skip("needs a local Redis")
    gate = PolitenessGate(client)
    domain = f"restart-{uuid.uuid4().hex}.test"
    try:
        first = await gate.acquire(domain, delay_ms=0, floor_delay_ms=0)
        assert first.allowed
        await gate.release(domain)

        await client.script_flush()

        second = await gate.acquire(domain, delay_ms=0, floor_delay_ms=0)
        assert second.allowed
        await gate.release(domain)
    finally:
        await client.delete(f"politeness:slots:{domain}", f"politeness:next:{domain}")
        await client.aclose()
