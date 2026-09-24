"""GET /v1/fetch — raw bytes through the impersonation tier, for the operator's own tools.

The web unblocker proxies pages for visitors; the page HTML comes from
/v1/scrape but its images, stylesheets and fonts are bytes, which no
scrape format carries. This hands those bytes back as-is, and with
`proxy=` it does so through a proxy of that type (and country), never the
engine's own address: if no proxy can be had, the fetch is refused rather
than sent direct.

Operator keys only (no owner): a customer key is refused, so the engine is
never an open fetch-anything service for the public API. Size-capped,
SSRF-checked, no browser tier. Proxy bytes are recorded against the
budget and the endpoint's score like any scrape.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from fastapi import APIRouter, Query, Response

from engine.api.deps import ApiKeyDep
from engine.core.credits import OPERATOR_OWNER
from engine.core.errors import EngineError, ErrorCode, Unauthorized
from engine.core.fetch.base import Fetcher, FetchRequest, FetchResult
from engine.core.fetch.tier1_impersonate import ImpersonateFetcher
from engine.core.ssrf import resolve_and_validate

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["fetch"])

MAX_BYTES = 10 * 1024 * 1024
_fetcher: Fetcher | None = None


def get_fetcher() -> Fetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = ImpersonateFetcher()
    return _fetcher


@router.get("/fetch")
async def fetch_bytes(
    key: ApiKeyDep,
    url: str = Query(min_length=8, max_length=2048),
    proxy: Literal["datacenter", "residential", "mobile"] | None = None,
    country: str | None = Query(default=None, min_length=2, max_length=2),
) -> Response:
    if key.owner_ref != OPERATOR_OWNER:
        raise Unauthorized("This endpoint is for operator keys only")
    target = await resolve_and_validate(url)

    endpoint: Any = None
    if proxy is not None:
        endpoint = await _select_proxy(target.host, proxy, country)

    request = FetchRequest(
        url=url,
        target=target,
        timeout_ms=20_000,
        block_assets=False,
        proxy_url=endpoint.connection_url() if endpoint else None,
        proxy_id=endpoint.id if endpoint else None,
        proxy_type=str(endpoint.type) if endpoint else None,
    )
    result = await get_fetcher().fetch(request)
    if endpoint is not None:
        await _report(endpoint.id, target.host, result)
    if result.error or result.status_code is None:
        raise EngineError(
            ErrorCode.INTERNAL, result.error or "The fetch returned nothing", {"url": url}
        )
    if len(result.body) > MAX_BYTES:
        raise EngineError(ErrorCode.INVALID_REQUEST, "The resource is over 10 MB")
    headers: dict[str, Any] = {
        "X-Final-URL": result.url,
        "X-Proxy-Type": request.proxy_type or "none",  # what it left through, by construction
        "Cache-Control": "private, max-age=3600",
    }
    return Response(
        content=result.body,
        status_code=result.status_code,
        media_type=result.content_type or "application/octet-stream",
        headers=headers,
    )


async def _select_proxy(domain: str, proxy: str, country: str | None) -> Any:
    """A proxy of the requested type, or an error — never a direct fetch."""
    try:
        from engine.core.proxy import budget, pool
        from engine.core.proxy.vendor import ProxyType
    except ImportError as exc:  # the open core without the proxy layer
        raise EngineError(ErrorCode.INTERNAL, "Proxies are not available on this engine") from exc

    ptype = ProxyType(proxy)
    try:
        await budget.check(ptype)
    except Exception as exc:  # noqa: BLE001 - BudgetExhausted or a store error: either way, refuse
        raise EngineError(ErrorCode.INTERNAL, f"No proxy bandwidth right now: {exc}") from exc
    endpoint = await pool.select(domain, ptype, country=country.lower() if country else None)
    if endpoint is None:
        raise EngineError(
            ErrorCode.INTERNAL, "No proxy available right now", {"type": proxy, "country": country}
        )
    return endpoint


async def _report(proxy_id: str, domain: str, result: FetchResult) -> None:
    try:
        from engine.core.proxy import budget, pool
    except ImportError:
        return
    ok = result.error is None and result.status_code is not None and result.status_code < 400
    try:
        await budget.record(
            proxy_id=proxy_id, domain=domain, bytes_used=result.bytes_transferred, success=ok
        )
        if result.status_code in (403, 429, 503):
            await pool.record_block(proxy_id, domain)
        elif ok:
            await pool.record_success(proxy_id, domain, result.latency_ms)
        else:
            await pool.record_failure(proxy_id, domain)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail the fetch
        logger.warning("fetch_proxy_report_failed", error=str(exc))
