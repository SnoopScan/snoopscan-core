"""Tier 1 — HTTP impersonation via curl_cffi (03-fetch-tiers.md section 3).

The workhorse. curl_cffi handles TLS (JA3/JA4), the HTTP/2 SETTINGS frame and
header ordering; our job is to not break that coherence.

Two rules that matter:
  - Never override User-Agent / Accept / Accept-Encoding / Sec-CH-UA*. The API
    rejects attempts before we get here; this module never sets them either.
  - Accept-Language must agree with proxy geography. A German exit IP sending
    en-US only is an anomaly, so we derive the language from location.country.

The impersonation profile is PINNED in settings, never floated. Floating means
our fingerprint changes silently when the library updates, which changes block
rates with no corresponding code change.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

from engine.core.fetch.base import FetchRequest, FetchResult, _is_internal
from engine.core.fetch.pinning import curl_resolve_entries
from engine.core.fetch.redirects import next_hop, strip_credentials_across_origin
from engine.core.geo import accept_language_for
from engine.core.models import Tier
from engine.core.redaction import redact
from engine.core.ssrf import resolve_and_validate
from engine.settings import settings


def _wire_bytes(response: object, headers: dict[str, str], body_len: int) -> int:
    """What the transfer actually cost on the wire.

    libcurl counts both directions for real: `request_size` is what we put out
    (method line, headers, any body) and `response_size` is what came back,
    headers included. The old figure was `len(body)` plus the headers — the
    DECOMPRESSED document, which is not what any proxy bills.

    Measured 18 Sep 2026 on one Wikipedia page: 315,345 decompressed against
    67,003 on the wire, 4.7x over. Overstating is not the safe direction; the
    daily proxy budget spends against this number.

    Falls back to the old estimate only if libcurl reports nothing, so a
    curl_cffi that stops exposing these degrades rather than reporting zero.
    """
    out = int(getattr(response, "request_size", 0) or 0)
    back = int(getattr(response, "response_size", 0) or 0)
    if out or back:
        return out + back
    header_bytes = sum(len(k) + len(v) + 4 for k, v in headers.items()) + 2
    return body_len + header_bytes


class ImpersonateFetcher:
    """Tier 1. Browser-grade TLS/HTTP2 fingerprint at HTTP cost."""

    name = str(Tier.IMPERSONATE)

    def __init__(self, profile: str | None = None, mobile_profile: str | None = None) -> None:
        self.profile = profile or settings.impersonate_profile
        self.mobile_profile = mobile_profile or settings.impersonate_profile_mobile

    async def fetch(self, req: FetchRequest) -> FetchResult:
        # Imported lazily: curl_cffi pulls a compiled extension, and keeping it
        # out of import time lets the rest of the engine be tested without it.
        from curl_cffi import CurlOpt
        from curl_cffi.requests import AsyncSession
        from curl_cffi.requests import errors as curl_errors

        started = time.monotonic()

        headers = dict(req.headers)
        # Only Accept-Language is ours to set, and only when geography implies
        # it. Everything else is owned by the impersonation profile.
        if "accept-language" not in {k.lower() for k in headers}:
            lang = accept_language_for(req.location)
            if lang:
                headers["Accept-Language"] = lang

        if req.mobile:
            # curl_cffi's android profile sends `Sec-Ch-Ua-Mobile: ?0` beside
            # a `Mobile Safari` User-Agent and `Sec-Ch-Ua-Platform: "Android"`
            # (measured against httpbin.org/headers, 9 Sep 2026). A real
            # Android Chrome sends `?1`. Correcting it follows the same rule
            # that makes these headers ours and not the caller's: a
            # fingerprint that contradicts itself is a detection signal, and
            # here it is the library doing the contradicting.
            headers["Sec-Ch-Ua-Mobile"] = "?1"

        try:
            async with AsyncSession() as session:
                # Redirects are walked here rather than by curl, so the SSRF
                # guard sees every hop. `allow_redirects=True` validated the
                # submitted URL and then followed whatever the server said —
                # a 302 into the cloud metadata endpoint went unchecked.
                url, method, hops = req.url, req.method, 0
                internal = _is_internal(urlsplit(req.url).hostname or "")
                while True:
                    if not internal:
                        target = await resolve_and_validate(url)
                        # Dial the address just judged rather than one the
                        # library resolves again afterwards. Direct only:
                        # through a proxy libcurl dials the PROXY and the
                        # target name travels inside the CONNECT, where there
                        # is nothing to pin.
                        if not req.proxy_url:
                            session.curl_options[CurlOpt.RESOLVE] = curl_resolve_entries(target)
                    response = await session.request(
                        method,
                        url,
                        headers=headers or None,
                        impersonate=self.mobile_profile if req.mobile else self.profile,
                        proxy=req.proxy_url,
                        timeout=req.timeout_ms / 1000,
                        allow_redirects=False,
                    )
                    hop = next_hop(
                        url, response.headers.get("location", ""), response.status_code, method
                    )
                    if hop is None:
                        break
                    hops += 1
                    if hops > settings.max_redirects:
                        return self._failure(
                            req, started, f"more than {settings.max_redirects} redirects", None
                        )
                    headers = strip_credentials_across_origin(headers, url, hop[0])
                    url, method = hop
        except curl_errors.RequestsError as exc:
            message = str(exc)
            kind = "timeout" if "timed out" in message.lower() else "request_error"
            # curl_cffi's exception carries the partial response libcurl built
            # before the failure (attached as .response), and libcurl itself
            # tracks download/upload size independent of whether the transfer
            # ever completed — a proxy CONNECT that gets torn down mid-TLS
            # still cost real bytes the vendor bills for, and until this the
            # ledger recorded every one of those as zero.
            return self._failure(req, started, redact(f"{kind}: {message}"), exc.response)
        except (OSError, ValueError) as exc:
            return self._failure(req, started, redact(f"{type(exc).__name__}: {exc}"), None)

        latency_ms = int((time.monotonic() - started) * 1000)
        body = response.content or b""
        headers_out = {k.lower(): v for k, v in dict(response.headers).items()}

        return FetchResult(
            url=str(response.url),
            status_code=response.status_code,
            headers=headers_out,
            body=body,
            content_type=headers_out.get("content-type"),
            tier=self.name,
            latency_ms=latency_ms,
            bytes_transferred=_wire_bytes(response, headers_out, len(body)),
            proxy_id=req.proxy_id,
            proxy_type=req.proxy_type,
        )

    def _failure(
        self, req: FetchRequest, started: float, error: str, partial: object | None
    ) -> FetchResult:
        transferred = 0
        if partial is not None:
            # response_size (download + header) is only what came BACK, and
            # is correctly 0 when nothing did — request_size is libcurl's own
            # count of what we actually put on the wire (method line, headers,
            # any body) before the failure, which is the half response_size
            # alone misses entirely. Measured against a real hung-server
            # timeout: response_size 0, request_size 77.
            transferred = int(getattr(partial, "request_size", 0) or 0) + int(
                getattr(partial, "download_size", 0) or 0
            )
        return FetchResult(
            url=req.url,
            status_code=None,
            headers={},
            body=b"",
            content_type=None,
            tier=self.name,
            latency_ms=int((time.monotonic() - started) * 1000),
            bytes_transferred=transferred,
            proxy_id=req.proxy_id,
            proxy_type=req.proxy_type,
            error=error,
        )

    async def healthcheck(self) -> bool:
        from curl_cffi.requests import AsyncSession

        try:
            async with AsyncSession() as session:
                response = await session.get(
                    "https://example.com", impersonate=self.profile, timeout=10
                )
        except Exception:  # noqa: BLE001 - healthcheck must never raise
            return False
        return bool(response.status_code and response.status_code < 500)
