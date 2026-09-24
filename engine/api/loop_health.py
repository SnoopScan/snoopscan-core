"""Event-loop lag, so the health endpoints can see saturation.

`/health` answered 200 throughout an outage in which 188 of 201 requests were
refused (pilot, 7 Sep 2026). It could not have done otherwise: it reported that
the process was alive, and the process WAS alive — pinned at 92% CPU with a
blocked event loop, answering whenever it won a slice while the accept queue
overflowed behind it.

Liveness cannot see that. What can is the loop's own lateness: a task that asks
to be woken every `INTERVAL_MS` and measures how late it actually is. A free
loop is late by a millisecond or two; a starved one is late by however long the
blocking call runs. That number is the difference between "alive" and "able to
take work", and it is the one a caller needs.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque

import structlog

logger = structlog.get_logger(__name__)

# How often to take the measurement. Frequent enough to catch a stall inside a
# single request, cheap enough to ignore.
INTERVAL_MS = 250
# Sustained lag above this and the instance should stop being given work. Set
# well above normal jitter (single-digit ms) and below the point at which a
# client's own timeouts start firing.
SATURATED_MS = 750
# What to tell a caller to do about it. Long enough for a stall to clear,
# short enough not to strand a client that could have been served.
RETRY_AFTER_SECONDS = 5
# Samples kept for the saturation decision. Judging on the LATEST sample alone
# makes the signal useless: a stall is over by the time the next sample lands,
# so a health check moments later reads a free loop and reports fine. Holding a
# couple of seconds means a stall is still visible when someone asks.
WINDOW = 8


class LoopMonitor:
    """Samples event-loop lag in the background."""

    def __init__(self) -> None:
        self.lag_ms: float = 0.0
        self.peak_lag_ms: float = 0.0
        self._recent: deque[float] = deque([0.0], maxlen=WINDOW)
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        interval = INTERVAL_MS / 1000
        while True:
            before = time.monotonic()
            await asyncio.sleep(interval)
            # Everything beyond the interval we asked for is time the loop was
            # not free to run us — which is time it was not accepting either.
            late_ms = max(0.0, (time.monotonic() - before - interval) * 1000)
            self.lag_ms = late_ms
            self._recent.append(late_ms)
            if late_ms > self.peak_lag_ms:
                self.peak_lag_ms = late_ms
            if late_ms > SATURATED_MS:
                logger.warning("event_loop_saturated", lag_ms=round(late_ms))

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def recent_lag_ms(self) -> float:
        """Worst lag in the window — what "can this take work" turns on."""
        return max(self._recent, default=0.0)

    @property
    def saturated(self) -> bool:
        return max(self.lag_ms, self.recent_lag_ms) > SATURATED_MS

    def snapshot(self) -> dict[str, float | bool]:
        return {
            "loopLagMs": round(self.lag_ms, 1),
            "recentLagMs": round(self.recent_lag_ms, 1),
            "peakLoopLagMs": round(self.peak_lag_ms, 1),
            "saturated": self.saturated,
        }


monitor = LoopMonitor()
