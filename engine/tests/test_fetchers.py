"""Tier fetchers against a mocked transport.

These exercise the REAL httpx code path. The escalation tests use scripted
stubs, which is why a live smoke run once caught a byte-accounting bug that the
stubs could not: nothing in the stub path touches httpx's header API.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from engine.core.fetch.base import FetchRequest
from engine.core.fetch.tier0_http import HttpFetcher
from engine.core.fetch.tier1_impersonate import ImpersonateFetcher
from engine.core.models import Location
from engine.settings import settings

BODY = b"<html><head><title>Hello</title></head><body><p>Content</p></body></html>"


@respx.mock
async def test_tier0_returns_body_and_status() -> None:
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(200, content=BODY, headers={"content-type": "text/html"})
    )
    fetcher = HttpFetcher()
    result = await fetcher.fetch(FetchRequest(url="https://example.com/page"))
    await fetcher.aclose()

    assert result.status_code == 200
    assert result.body == BODY
    assert result.tier == "http"
    assert result.error is None


@respx.mock
async def test_bytes_transferred_is_measured_not_estimated() -> None:
    """Constraint C4: proxy bytes are the dominant real cost and must be
    measured. This asserts header bytes are counted on top of the body, using
    httpx's raw header API — the exact call that broke once in production
    while every stubbed test still passed.
    """
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200,
            content=BODY,
            headers={"content-type": "text/html", "x-extra": "some-header-value"},
        )
    )
    fetcher = HttpFetcher()
    result = await fetcher.fetch(FetchRequest(url="https://example.com/page"))
    await fetcher.aclose()

    assert result.bytes_transferred > len(BODY), "header bytes must be included"
    # Sanity ceiling: headers here are small, so the total cannot be wild.
    assert result.bytes_transferred < len(BODY) + 500


@respx.mock
async def test_tier0_sends_an_honest_user_agent() -> None:
    """No browser UA at tier 0. A real Chrome UA on a request carrying a Python
    TLS fingerprint is a WORSE signal than an honest one — the mismatch is
    exactly what fingerprint checks look for."""
    route = respx.get("https://example.com/page").mock(
        return_value=httpx.Response(200, content=BODY)
    )
    fetcher = HttpFetcher()
    await fetcher.fetch(FetchRequest(url="https://example.com/page"))
    await fetcher.aclose()

    sent = route.calls.last.request.headers["user-agent"]
    assert sent == settings.user_agent
    assert "Mozilla" not in sent
    assert "Chrome" not in sent


@respx.mock
async def test_accept_language_follows_location() -> None:
    """Geographic coherence: language headers must agree with the geography we
    claim to be in."""
    route = respx.get("https://example.com/page").mock(
        return_value=httpx.Response(200, content=BODY)
    )
    fetcher = HttpFetcher()
    await fetcher.fetch(
        FetchRequest(
            url="https://example.com/page",
            location=Location(country="DE", languages=["de-DE"]),
        )
    )
    await fetcher.aclose()

    assert "de-DE" in route.calls.last.request.headers["accept-language"]


@respx.mock
async def test_network_error_becomes_a_failure_result_not_an_exception() -> None:
    """The escalation controller decides what to do about a failure; a fetcher
    that raises would bypass that decision entirely."""
    respx.get("https://example.com/page").mock(side_effect=httpx.ConnectError("refused"))
    fetcher = HttpFetcher()
    result = await fetcher.fetch(FetchRequest(url="https://example.com/page"))
    await fetcher.aclose()

    assert result.error is not None
    assert result.status_code is None
    assert result.bytes_transferred == 0


@respx.mock
async def test_timeout_is_reported_as_a_timeout() -> None:
    respx.get("https://example.com/page").mock(side_effect=httpx.ReadTimeout("slow"))
    fetcher = HttpFetcher()
    result = await fetcher.fetch(FetchRequest(url="https://example.com/page", timeout_ms=1000))
    await fetcher.aclose()

    assert result.error is not None
    assert "timeout" in result.error.lower()


@respx.mock
async def test_final_url_after_redirect_is_reported() -> None:
    respx.get("https://example.com/old").mock(
        return_value=httpx.Response(301, headers={"location": "https://example.com/new"})
    )
    respx.get("https://example.com/new").mock(return_value=httpx.Response(200, content=BODY))
    fetcher = HttpFetcher()
    result = await fetcher.fetch(FetchRequest(url="https://example.com/old"))
    await fetcher.aclose()

    assert result.url == "https://example.com/new"


def test_impersonation_profile_is_pinned_not_floating() -> None:
    """A floating profile means our fingerprint changes silently when the
    library updates, changing block rates with no code change to point at."""
    fetcher = ImpersonateFetcher()
    assert fetcher.profile == settings.impersonate_profile
    assert any(ch.isdigit() for ch in fetcher.profile), (
        f"profile {fetcher.profile!r} must pin a version, e.g. 'chrome124'"
    )


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("text/html; charset=utf-8", "café"),
        ("text/html; charset=iso-8859-1", "cafÃ©"),
    ],
)
async def test_text_decoding_honours_charset(content_type: str, expected: str) -> None:
    from engine.core.fetch.base import FetchResult

    result = FetchResult(
        url="https://example.com/",
        status_code=200,
        headers={},
        body="café".encode(),
        content_type=content_type,
        tier="http",
        latency_ms=1,
        bytes_transferred=10,
    )
    assert expected in result.text()


async def _hangs_after_reading(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.read(4096)
    await asyncio.sleep(10)
    writer.close()


async def test_tier1_reports_real_bytes_on_a_timeout_not_zero() -> None:
    """curl_cffi's exception carries a partial Response with libcurl's own
    request_size — a bodyless GET's bytes never show up on download_size
    (nothing came back) or upload_size (curl only counts a request BODY as
    "upload"), only on request_size. Using the wrong field silently reads
    back as zero, same bug as never reading it at all — checked against a
    real hung connection, not asserted from documentation."""
    server = await asyncio.start_server(_hangs_after_reading, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        task = asyncio.ensure_future(server.serve_forever())
        try:
            fetcher = ImpersonateFetcher()
            req = FetchRequest(url=f"http://127.0.0.1:{port}/", timeout_ms=500)
            result = await fetcher.fetch(req)
            assert result.error is not None
            assert result.bytes_transferred > 0
        finally:
            task.cancel()


def test_a_proxied_fetch_bills_the_socket_not_the_unzipped_body() -> None:
    """The invoice counts wire bytes; the decompressed body is a different number.

    Measured against a provider's usage API, Sep 2026: one Wikipedia page through a
    residential exit billed 74,451 bytes while the engine recorded 321,783 —
    4.3x over, because the page ships gzipped and `_measured_bytes` counts it
    unzipped. That figure is what the daily proxy budget spends against, so
    overstating it stops a day's work early.
    """
    import httpx

    from engine.core.fetch.byte_meter import ByteCounter
    from engine.core.fetch.tier0_http import _billable_bytes

    body = b"x" * 300_000  # a big decompressed document
    response = httpx.Response(200, content=body, headers={"content-type": "text/html"})

    # Proxied: the socket counter is the answer, however large the body is.
    meter = ByteCounter(down=70_000, up=4_451)
    assert _billable_bytes(response, meter) == 74_451

    # Direct: no counter is built, so the measured estimate stands.
    assert _billable_bytes(response, None) > 300_000


def test_tier1_bills_libcurls_wire_count_not_the_unzipped_body() -> None:
    """Same fault as tier 0, same direction: gzipped pages counted unzipped.

    Measured 18 Sep 2026 on one Wikipedia page through curl_cffi: 315,345
    bytes decompressed against 67,003 actually on the wire (request_size 678 +
    response_size 66,325). libcurl has the real figures on success, not only
    on the failure path where they were already being read.
    """
    from engine.core.fetch.tier1_impersonate import _wire_bytes

    class Response:
        request_size = 678
        response_size = 66_325

    headers = {"content-type": "text/html", "content-encoding": "gzip"}
    assert _wire_bytes(Response(), headers, body_len=315_345) == 67_003

    # If libcurl reports nothing, degrade to the old estimate rather than zero:
    # a silent zero would understate the bill and the budget both.
    class Silent:
        request_size = 0
        response_size = 0

    assert _wire_bytes(Silent(), headers, body_len=1_000) > 1_000
