"""Tier 0 — plain HTTP via httpx (03-fetch-tiers.md section 2).

Used for robots.txt, sitemaps, cooperative domains and JSON APIs.

Deliberately does NOT spoof a browser User-Agent. A real Chrome UA on a request
carrying a Python TLS fingerprint is a *worse* signal than an honest one — the
mismatch is exactly what fingerprint checks look for. We identify honestly with
a contact URL.
"""

from __future__ import annotations

import time

import httpx

from engine.core.fetch.base import FetchRequest, FetchResult, _is_internal
from engine.core.fetch.byte_meter import ByteCounter, CountingTransport
from engine.core.fetch.pinning import PinnedBackend
from engine.core.fetch.redirects import follow as follow_redirects
from engine.core.geo import accept_language_for
from engine.core.models import Tier
from engine.core.redaction import redact
from engine.settings import settings


def _measured_bytes(response: httpx.Response) -> int:
    """Body plus header bytes actually received.

    httpx exposes no raw socket counter, so header size is computed from the
    real raw header bytes (`Headers.raw` is a list of byte pairs) rather than
    guessed at. The `+ 4` per header is `": "` and CRLF; the trailing `+ 2` is
    the blank line ending the header block. Body length is exact.
    """
    header_bytes = sum(len(name) + len(value) + 4 for name, value in response.headers.raw) + 2
    status_line = len(response.http_version) + 12
    return len(response.content) + header_bytes + status_line


def _billable_bytes(response: httpx.Response, meter: ByteCounter | None) -> int:
    """What this fetch actually cost on the wire.

    Through a proxy the raw socket counter wins, and it was already running —
    CountingTransport exists for exactly this — but it was only ever read on the
    FAILURE path. A success reported `_measured_bytes`, which counts the
    DECOMPRESSED body and estimates headers, and knows nothing of the TLS
    handshake or the CONNECT tunnel the vendor also bills.

    Measured against a proxy provider's own usage API, Sep 2026, one Wikipedia page
    through a residential exit: the invoice said 74,451 bytes, the engine
    recorded 321,783 — 4.3x over, because the page ships gzipped and we were
    counting it unzipped. Overstating proxy bytes is not the harmless
    direction: it is the number the daily proxy budget spends against, so the
    budget stops a day's work roughly four times too early.

    Direct fetches have no counter (one is only built for a proxied request),
    and they cost no vendor bytes, so the decompressed estimate stands there.
    """
    if meter is not None:
        return meter.total
    return _measured_bytes(response)


def _is_internal_target(req: FetchRequest) -> bool:
    """Our own services, not a caller's URL: the search backend on loopback
    is put there on purpose and the SSRF guard exists to refuse exactly it."""
    from urllib.parse import urlsplit

    return _is_internal(urlsplit(req.url).hostname or "")


class HttpFetcher:
    """Tier 0. Honest, cheap, no fingerprint work."""

    name = str(Tier.HTTP)

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                http2=True,
                # Walked by fetch/redirects.py so every hop is validated.
                follow_redirects=False,
                limits=httpx.Limits(
                    max_connections=settings.http_max_connections,
                    max_keepalive_connections=settings.http_max_keepalive,
                ),
                timeout=httpx.Timeout(
                    settings.connect_timeout_ms / 1000,
                    connect=settings.connect_timeout_ms / 1000,
                ),
                headers={"User-Agent": settings.user_agent},
            )
        return self._client

    async def fetch(self, req: FetchRequest) -> FetchResult:
        client = await self._get_client()
        started = time.monotonic()

        headers = dict(req.headers)
        headers.setdefault("User-Agent", settings.user_agent)
        # `accept_language_for`, the same helper tier 1 uses. Tier 0 read
        # `location.languages` DIRECTLY, so a caller who set only a country —
        # which is the whole of what the API documents — got no
        # Accept-Language at all here and the right one a rung up. Two tiers
        # answering the same question differently, with the cheapest and
        # busiest one giving the wrong answer.
        language = accept_language_for(req.location)
        if language:
            headers["Accept-Language"] = language

        timeout = httpx.Timeout(
            req.timeout_ms / 1000,
            connect=min(settings.connect_timeout_ms, req.timeout_ms) / 1000,
        )

        # A proxied request needs its own client: httpx binds the proxy to the
        # transport, not the request. It also gets a byte-counting transport —
        # a proxy is the one case bytes are billed for, success or not, and
        # httpx itself exposes no counter (see byte_meter.py).
        proxied: httpx.AsyncClient | None = None
        meter: ByteCounter | None = None
        if req.proxy_url:
            transport = CountingTransport(proxy=req.proxy_url)
            meter = transport.counter
            proxied = httpx.AsyncClient(
                http2=True,
                follow_redirects=False,
                transport=transport,
                headers={"User-Agent": settings.user_agent},
            )
            client = proxied

        # A pinned client for the direct path: it dials the address the SSRF
        # guard validated and refuses any authority nobody pinned. Built per
        # request, because a pin table shared across jobs outlives the
        # validation it came from.
        pinned: httpx.AsyncClient | None = None
        backend: PinnedBackend | None = None
        if not req.proxy_url and req.target is not None:
            backend = PinnedBackend()
            backend.pin_target(req.target)
            transport_p = httpx.AsyncHTTPTransport(http2=True)
            transport_p._pool._network_backend = backend  # noqa: SLF001 - the documented seam
            pinned = httpx.AsyncClient(
                http2=True,
                follow_redirects=False,
                transport=transport_p,
                headers={"User-Agent": settings.user_agent},
            )
            client = pinned

        try:
            outgoing = client.build_request(
                req.method,
                req.url,
                headers=headers,
                content=req.body.encode() if req.body else None,
            )
            response = await follow_redirects(
                client,
                outgoing,
                max_redirects=settings.max_redirects,
                timeout=timeout,
                extensions=({"sni_hostname": req.target.host} if req.target else None),
                # An internal host is ours on purpose — the search backend on
                # loopback — and the guard is there to refuse exactly that.
                validate=not _is_internal_target(req),
                pin=backend.pin_target if backend is not None else None,
            )
        except httpx.TimeoutException as exc:
            # redact(): a transport exception can carry the full proxy URL,
            # credentials included, and this string is logged and stored.
            return self._failure(req, started, redact(f"timeout: {exc}"), meter)
        except httpx.HTTPError as exc:
            return self._failure(req, started, redact(f"{type(exc).__name__}: {exc}"), meter)
        finally:
            if proxied is not None:
                await proxied.aclose()
            if pinned is not None:
                await pinned.aclose()

        latency_ms = int((time.monotonic() - started) * 1000)
        measured = _billable_bytes(response, meter)
        return FetchResult(
            url=str(response.url),
            status_code=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.content,
            content_type=response.headers.get("content-type"),
            tier=self.name,
            latency_ms=latency_ms,
            bytes_transferred=measured,
            proxy_id=req.proxy_id,
            proxy_type=req.proxy_type,
        )

    def _failure(
        self, req: FetchRequest, started: float, error: str, meter: ByteCounter | None
    ) -> FetchResult:
        return FetchResult(
            url=req.url,
            status_code=None,
            headers={},
            body=b"",
            content_type=None,
            tier=self.name,
            latency_ms=int((time.monotonic() - started) * 1000),
            bytes_transferred=meter.total if meter is not None else 0,
            proxy_id=req.proxy_id,
            proxy_type=req.proxy_type,
            error=error,
        )

    async def healthcheck(self) -> bool:
        try:
            client = await self._get_client()
            response = await client.get("https://example.com", timeout=10.0)
        except httpx.HTTPError:
            return False
        return response.status_code < 500

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
