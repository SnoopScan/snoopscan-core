"""The Redis client is never used, or closed, from a loop that did not make it.

It is a module-level singleton, which is right in production (one loop for the
life of the process) and wrong across tests: one test's client, bound to its
now-closed loop, was inherited by the next, and the app's shutdown then called
aclose() on it and died with "Event loop is closed". Which tests failed depended
purely on ORDER — six passed alone and failed in the exported suite, where the
withheld tests that used to separate them were gone (Sep 2026).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from engine.core import politeness


class _StaleClient:
    closed = False

    async def aclose(self) -> None:
        _StaleClient.closed = True
        raise RuntimeError("Event loop is closed")


@pytest.fixture(autouse=True)
def _reset() -> Any:
    politeness._redis = None
    politeness._redis_loop = None
    yield
    politeness._redis = None
    politeness._redis_loop = None


def _from_another_loop() -> None:
    other = asyncio.new_event_loop()
    other.close()
    politeness._redis = _StaleClient()  # type: ignore[assignment]
    politeness._redis_loop = other


async def test_closing_a_client_from_another_loop_does_not_raise() -> None:
    _from_another_loop()
    await politeness.close_redis()
    assert politeness._redis is None
    assert _StaleClient.closed is False, "it must not even try: that loop is gone"


async def test_a_client_from_another_loop_is_replaced_not_reused() -> None:
    _from_another_loop()
    stale = politeness._redis
    client = await politeness.get_redis()
    assert client is not stale
    assert politeness._redis_loop is asyncio.get_running_loop()
    await politeness.close_redis()


async def test_the_same_loop_keeps_its_one_client() -> None:
    """Production: one loop, one client, reused."""
    first = await politeness.get_redis()
    assert await politeness.get_redis() is first
    await politeness.close_redis()
