"""A worker's blocking pop must never be cut short by the Redis read timeout.

redis-py 5+ defaults socket_timeout to 5 s. Before the fix, `BRPOP ... 5` on an
idle queue raised TimeoutError on the read and the HTTP worker died after its
first five idle seconds — in production, every worker, every quiet minute.
"""

from __future__ import annotations

import asyncio

import pytest

from engine.core import politeness
from engine.workers import queue as queue_mod


@pytest.fixture(autouse=True)
def _fresh_client():
    politeness._redis = None
    yield
    politeness._redis = None


def test_redis_client_read_timeout_outlasts_any_blocking_pop() -> None:
    client = asyncio.run(politeness.get_redis())
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == politeness.REDIS_SOCKET_TIMEOUT
    assert kwargs["socket_timeout"] > politeness.MAX_BLOCKING_SECONDS
    assert kwargs["socket_connect_timeout"] == politeness.REDIS_CONNECT_TIMEOUT
    asyncio.run(client.aclose())


def test_worker_block_time_is_under_the_cap() -> None:
    # The HTTP worker asks for 5 s; the cap must keep that legal and forbid more.
    assert 5 <= politeness.MAX_BLOCKING_SECONDS < politeness.REDIS_SOCKET_TIMEOUT


def test_pop_refuses_a_block_time_the_read_timeout_would_abort() -> None:
    q = queue_mod.JobQueue()
    with pytest.raises(ValueError, match="MAX_BLOCKING_SECONDS"):
        asyncio.run(q.pop(block_seconds=politeness.MAX_BLOCKING_SECONDS + 1))
