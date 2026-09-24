"""Extraction must not run on the API's event loop.

`extract()` is CPU-bound — trafilatura and selectolax walk the whole document.
Called inline from `async def scrape`, one large page blocks every other
request AND the accept loop, so new connections are refused while `/health`
still answers 200 whenever it wins a slice.

That is not hypothetical. The pilot's overnight run on 7 Sep 2026 lost 188 of
201 names to `UND_ERR_SOCKET` then `ECONNREFUSED`, against a single uvicorn
process pinned at 92.4% CPU that recovered on its own once the queue drained.
Measured here afterwards with 12 concurrent scrapes of real pages: `/health`
median 114 ms and peak 549 ms when extraction ran inline, 7 ms and 41 ms once
it moved to a thread.

`/v1/extract` and `/v1/parse` already used `asyncio.to_thread`. `/v1/scrape`,
the busiest path, did not.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from engine.core.fetch.base import FetchRequest, FetchResult
from engine.core.models import ScrapeOptions, Tier
from engine.core.scrape_service import ScrapeService
from engine.tests.fixtures.builders import article_html

# Long enough to be unmistakable against a 10 ms tick, short enough to keep the
# suite quick.
_EXTRACT_MS = 300
_TICK_MS = 10


class _StubFetcher:
    name = "http"

    async def fetch(self, req: FetchRequest) -> FetchResult:
        body = article_html().encode()
        return FetchResult(
            url=req.url,
            status_code=200,
            headers={},
            body=body,
            content_type="text/html; charset=utf-8",
            tier=self.name,
            latency_ms=1,
            bytes_transferred=len(body),
        )

    async def healthcheck(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_extraction_does_not_block_the_event_loop(monkeypatch: Any) -> None:
    from engine.core.extract.router import extract as real_extract

    def slow_extract(*args: Any, **kwargs: Any) -> Any:
        # time.sleep stands in for CPU work: like the real extractor's C
        # extensions it releases the GIL, so it is only harmless when it runs
        # somewhere other than the loop.
        time.sleep(_EXTRACT_MS / 1000)
        return real_extract(*args, **kwargs)

    monkeypatch.setattr("engine.core.scrape_service.extract", slow_extract)

    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(_TICK_MS / 1000)

    service = ScrapeService({Tier.HTTP: _StubFetcher()}, persist=False)
    task = asyncio.create_task(ticker())
    try:
        await service.scrape("https://example.com/post", ScrapeOptions())
    finally:
        stop.set()
        await task

    # A free loop manages ~30 ticks across a 300 ms extraction. A blocked one
    # manages almost none. Half is a wide margin that still separates them.
    expected = _EXTRACT_MS / _TICK_MS
    assert ticks > expected * 0.5, (
        f"event loop starved during extraction: {ticks} ticks, expected "
        f"~{expected:.0f}. Extraction is running on the loop again."
    )
