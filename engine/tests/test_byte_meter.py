"""A failed proxied fetch still cost real bytes, and the ledger must know it.

`respx` (used throughout test_fetchers.py) mocks at httpx's transport
dispatch layer, before a request ever reaches the network — exactly the
layer this fix adds instrumentation to. Proving it needs a real socket, so
these tests run a local TCP server that accepts a connection, reads what was
sent, and then either answers or hangs — no external network dependency, but
real bytes crossing a real loopback socket.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from engine.core.fetch.byte_meter import ByteCounter, CountingTransport


async def _hangs_after_reading(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Accepts the request, never answers — the read side times out."""
    await reader.read(4096)
    await asyncio.sleep(10)
    writer.close()


async def _answers_normally(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.read(4096)
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello")
    await writer.drain()
    writer.close()


@pytest.fixture
async def hanging_server() -> AsyncIterator[int]:
    server = await asyncio.start_server(_hangs_after_reading, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        task = asyncio.ensure_future(server.serve_forever())
        yield port
        task.cancel()


@pytest.fixture
async def answering_server() -> AsyncIterator[int]:
    server = await asyncio.start_server(_answers_normally, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        task = asyncio.ensure_future(server.serve_forever())
        yield port
        task.cancel()


async def test_counts_real_bytes_on_a_successful_request(answering_server: int) -> None:
    transport = CountingTransport()
    async with httpx.AsyncClient(transport=transport) as client:
        r = await client.get(f"http://127.0.0.1:{answering_server}/")
    assert r.status_code == 200
    assert transport.counter.up > 0
    assert transport.counter.down > 0


async def test_counts_bytes_sent_even_when_the_response_never_arrives(hanging_server: int) -> None:
    """The case this fix exists for: a request the network genuinely cost
    bytes for, that never got a response — previously recorded as zero."""
    transport = CountingTransport()
    async with httpx.AsyncClient(transport=transport, timeout=1.0) as client:
        with pytest.raises(httpx.TimeoutException):
            await client.get(f"http://127.0.0.1:{hanging_server}/")
    # The request itself (method line, headers) really did cross the socket.
    assert transport.counter.up > 0
    assert transport.counter.down == 0


async def test_counts_nothing_when_the_connection_itself_never_opens() -> None:
    """No TCP handshake ever completed, so nothing was genuinely spent —
    this must stay zero, not become a flat estimate."""
    transport = CountingTransport()
    async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
        # Port 1 on loopback: nothing listens. Normally the refusal is
        # instant, but a loaded box can let the timeout win that race — and
        # either way no handshake completed, which is the whole assertion.
        # Pinned to ConnectError, it failed once inside the full suite while
        # passing alone (20 Sep 2026).
        with pytest.raises(httpx.TransportError):
            await client.get("http://127.0.0.1:1/")
    assert transport.counter.total == 0


def test_byte_counter_totals_up_and_down() -> None:
    c = ByteCounter(down=100, up=50)
    assert c.total == 150
