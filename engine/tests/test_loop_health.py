"""Health must be able to say "alive but cannot take work".

`/health` answered 200 throughout the outage that lost 188 of 201 requests
(pilot, 7 Sep 2026). The process was alive; the event loop was blocked. A
liveness check cannot express that difference, and the caller needed it.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from engine.api.loop_health import (
    RETRY_AFTER_SECONDS,
    SATURATED_MS,
    LoopMonitor,
)


@pytest.mark.asyncio
async def test_a_free_loop_reports_almost_no_lag() -> None:
    m = LoopMonitor()
    m.start()
    await asyncio.sleep(1.0)
    await m.stop()
    assert not m.saturated
    assert m.lag_ms < SATURATED_MS


@pytest.mark.asyncio
async def test_a_blocked_loop_is_reported_as_saturated() -> None:
    """The measurement that matters: synchronous work on the loop."""
    m = LoopMonitor()
    m.start()
    await asyncio.sleep(0.3)
    time.sleep((SATURATED_MS + 500) / 1000)  # noqa: ASYNC251 - blocking IS the test; asyncio.sleep would yield instead
    await asyncio.sleep(0.3)
    saturated = m.saturated
    peak = m.peak_lag_ms
    await m.stop()
    assert saturated, f"a blocked loop must read as saturated, lag was {peak}ms"
    assert peak > SATURATED_MS


@pytest.mark.asyncio
async def test_the_snapshot_carries_the_number_not_just_a_flag() -> None:
    """An operator asking "why is nothing processing" should not have to
    guess; a boolean cannot be trended."""
    m = LoopMonitor()
    snap = m.snapshot()
    assert set(snap) == {"loopLagMs", "recentLagMs", "peakLoopLagMs", "saturated"}


def test_ready_tells_a_saturated_caller_when_to_come_back() -> None:
    from fastapi.testclient import TestClient

    from engine.api.app import app
    from engine.api.loop_health import monitor

    monitor.lag_ms = SATURATED_MS + 1000
    monitor._recent.append(SATURATED_MS + 1000)
    try:
        with TestClient(app) as client:
            response = client.get("/ready")
            assert response.status_code == 503
            assert response.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)
            assert response.json()["checks"]["eventLoop"] is False
    finally:
        monitor.lag_ms = 0.0
        monitor._recent.clear()
        monitor._recent.append(0.0)


def test_health_reports_capacity_alongside_liveness() -> None:
    from fastapi.testclient import TestClient

    from engine.api.app import app

    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "loopLagMs" in body, "the field the outage needed and did not have"
