"""FastAPI application — the REST surface from 01-api-surface.md.

Everything returns the standard envelope: `{success, data}` or
`{success, error}`. Unknown request fields are a 400, never ignored.
"""

from __future__ import annotations

import functools
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from engine import __version__
from engine.api import deps
from engine.api.loop_health import RETRY_AFTER_SECONDS
from engine.api.loop_health import monitor as loop_monitor
from engine.api.routes import company as company_routes
from engine.api.routes import crawl as crawl_routes
from engine.api.routes import domain as domain_routes
from engine.api.routes import extract as extract_routes
from engine.api.routes import fetch as fetch_routes
from engine.api.routes import internal as internal_routes
from engine.api.routes import leads as leads_routes
from engine.api.routes import monitor as monitor_routes
from engine.api.routes import parse as parse_routes
from engine.api.routes import places as places_routes
from engine.api.routes import platforms as platforms_routes
from engine.api.routes import scrape as scrape_routes
from engine.api.routes import serp as serp_routes
from engine.api.routes import source as source_routes
from engine.api.routes import templates as templates_routes
from engine.core.errors import EngineError, ErrorCode
from engine.core.models import ScrapeOptions, Tier
from engine.core.politeness import close_redis, get_redis
from engine.logging_config import configure_logging
from engine.mcp import http as mcp_http
from engine.settings import settings
from engine.storage import db

# Before anything has a chance to log. A log line emitted during import would
# otherwise go out through structlog's defaults, unredacted.
configure_logging(level=settings.log_level)

logger = structlog.get_logger(__name__)

# Computed from the model, never typed out: a new ScrapeOptions field is
# covered by this hint the moment it exists.
_SCRAPE_OPTION_FIELDS = frozenset(ScrapeOptions.model_fields) - {"url"}


log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Before anything else: a deployment that forbids direct egress and has no
    # proxy can serve nothing. Refuse to start rather than answer every request
    # with a 503 that looks like a target problem.
    settings.assert_egress_is_servable()
    try:
        from engine.core.proxy import providers

        await providers.refresh()
    except ImportError:
        log.info("proxy_registry_absent", reason="open core without the proxy layer")
    except Exception as exc:  # noqa: BLE001 — a registry outage must not stop the API
        log.warning("proxy_registry_load_failed", error=str(exc))

    # The desk's extraction templates, same reasoning: absent is fine, stale is
    # not — a template edited on the desk must be live without a restart.
    try:
        from engine.core.extract import templates as _templates

        await _templates.refresh()
    except Exception as exc:  # noqa: BLE001 — extraction must survive this
        log.warning("templates_load_failed", error=str(exc))
    loop_monitor.start()
    async with mcp_http.lifespan():
        yield
    await loop_monitor.stop()
    await db.close_pool()
    await close_redis()
    http = deps.get_fetchers().get(Tier.HTTP)
    if http is not None and hasattr(http, "aclose"):
        await http.aclose()


app = FastAPI(
    # The product's name, not the repository's: API directories list this.
    title="SnoopScan API",
    description=(
        "Turn any public web page into clean markdown or structured JSON: scrape, "
        "crawl, map, search and extract. One bearer key for every endpoint."
    ),
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)


def _public_openapi() -> dict[str, Any]:
    """The published schema, with everything marked internal taken back out.

    Marking a FIELD `x-internal` keeps it off the reference, but Pydantic still
    emits the TYPE it points at — so `tier` was hidden while the Tier enum sat
    in components/schemas publishing every rung of the ladder to anyone who
    fetched /openapi.json. Hiding the door is not hiding the room.

    So: drop internal properties, then drop any component nothing references
    any more, repeatedly, until the document stops shrinking.
    """
    from fastapi.openapi.utils import get_openapi

    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
        description=app.description,
        servers=[{"url": settings.public_api_url.rstrip("/")}],
    )
    components = (schema.get("components") or {}).get("schemas") or {}

    # 1. Internal properties never appear.
    for definition in components.values():
        props = definition.get("properties")
        if not isinstance(props, dict):
            continue
        for name in [
            n for n, spec in props.items() if isinstance(spec, dict) and spec.get("x-internal")
        ]:
            props.pop(name, None)
            required = definition.get("required")
            if isinstance(required, list) and name in required:
                required.remove(name)

    # 2. Then anything nothing points at, which is how the enum leaked.
    def referenced(node: Any, out: set[str]) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                out.add(ref.rsplit("/", 1)[1])
            for value in node.values():
                referenced(value, out)
        elif isinstance(node, list):
            for value in node:
                referenced(value, out)

    while True:
        live: set[str] = set()
        referenced(schema.get("paths") or {}, live)
        for name in list(live):
            referenced(components.get(name) or {}, live)
        orphans = set(components) - live
        if not orphans:
            break
        for name in orphans:
            components.pop(name, None)

    app.openapi_schema = schema
    return schema


app.openapi = _public_openapi  # type: ignore[method-assign]

app.include_router(scrape_routes.router, prefix="/v1")
app.include_router(domain_routes.router, prefix="/v1")
app.include_router(company_routes.router, prefix="/v1")
app.include_router(platforms_routes.router, prefix="/v1")
app.include_router(monitor_routes.router, prefix="/v1")
app.include_router(crawl_routes.router, prefix="/v1")
app.include_router(extract_routes.router, prefix="/v1")
app.include_router(parse_routes.router, prefix="/v1")
app.include_router(fetch_routes.router, prefix="/v1")
app.include_router(source_routes.router, prefix="/v1")
app.include_router(places_routes.router, prefix="/v1")
app.include_router(leads_routes.router, prefix="/v1")
app.include_router(serp_routes.router, prefix="/v1")
app.include_router(templates_routes.router, prefix="/v1")
# The control plane is for this application, not for customers: keep it out of the
# public schema at /openapi.json and /docs, which the members area links to.
app.include_router(internal_routes.router, include_in_schema=False)

# The hosted MCP endpoint: the same tools an agent gets over stdio, behind the
# same bearer keys, rate limits and metering as /v1. See engine.mcp.http.
if settings.mcp_http_enabled:
    from engine.mcp.server import mcp as mcp_server

    # A raw ASGI app on an exact path. `mount` is the type-clean way to attach
    # one, and it is WRONG here: Starlette's Mount treats "/mcp" as a prefix and
    # redirects the bare path to "/mcp/", which the MCP transport does not
    # follow — measured, it broke five transport tests. add_route keeps the
    # exact path, and the signature mismatch is Starlette's typing being
    # narrower than what it accepts at runtime.
    app.add_route(
        "/mcp",
        mcp_http.build_asgi(mcp_server),  # type: ignore[arg-type]
        methods=["GET", "POST", "DELETE"],
        include_in_schema=False,
    )
    # The same server for apps that sign in instead of taking a key — the
    # Claude app's connectors take a URL and nothing else. No key here means
    # a 401 pointing at the sign-in, not the keyless listing /mcp gives.
    app.add_route(
        mcp_http.SIGN_IN_PATH,
        mcp_http.build_asgi(mcp_server, sign_in=True),  # type: ignore[arg-type]
        methods=["GET", "POST", "DELETE"],
        include_in_schema=False,
    )
    _meta = mcp_http.PROTECTED_RESOURCE_PATH
    for _path in (_meta, _meta + mcp_http.SIGN_IN_PATH):
        app.add_route(_path, mcp_http.protected_resource, methods=["GET"], include_in_schema=False)


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------


@app.middleware("http")
async def request_context(request: Request, call_next: Any) -> Response:
    trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex
    request.state.trace_id = trace_id
    started = time.monotonic()

    response: Response = await call_next(request)

    response.headers["X-Trace-Id"] = trace_id
    limits = getattr(request.state, "rate_limit", None)
    if limits:
        limit, remaining, reset = limits
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset)

    logger.info(
        "request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=int((time.monotonic() - started) * 1000),
        trace_id=trace_id,
    )
    return response


# --------------------------------------------------------------------------
# Error handling — one envelope, every path
# --------------------------------------------------------------------------


@app.exception_handler(EngineError)
async def engine_error_handler(request: Request, exc: EngineError) -> JSONResponse:
    headers: dict[str, str] = {}
    if exc.code == ErrorCode.RATE_LIMITED:
        headers["Retry-After"] = str(exc.detail.get("retry_after", 60))
    return JSONResponse(
        status_code=exc.http_status,
        content={"success": False, "error": exc.to_payload()},
        headers=headers,
    )


@functools.lru_cache(maxsize=1)
def _paths_taking_scrape_options() -> frozenset[str]:
    """The routes whose request body actually has a `scrapeOptions` object.

    From the OpenAPI schema, once, so a new route is covered without a list to
    keep in step. The hint below used to fire on every endpoint and told
    /v1/serp callers to move `actions` into a scrapeOptions it does not have.
    """
    schema = app.openapi()
    models = schema.get("components", {}).get("schemas", {})
    found: set[str] = set()
    for path, operations in schema.get("paths", {}).items():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            body = operation.get("requestBody", {}).get("content", {})
            ref = body.get("application/json", {}).get("schema", {}).get("$ref", "")
            if "scrapeOptions" in models.get(ref.rsplit("/", 1)[-1], {}).get("properties", {}):
                found.add(path)
    return frozenset(found)


def _problem(err: dict[str, Any], path: str = "") -> dict[str, str]:
    field = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
    message = err.get("msg", "invalid")
    # "Extra inputs are not permitted" is true and useless when the field is
    # real and simply one level up. `/v1/batch/scrape` takes `formats` and
    # `onlyMainContent` inside `scrapeOptions`, and told callers they did not
    # exist (measured, Sep 2026). Only where scrapeOptions exists, though.
    if (
        err.get("type") == "extra_forbidden"
        and field in _SCRAPE_OPTION_FIELDS
        and path in _paths_taking_scrape_options()
    ):
        message = (
            f"'{field}' is a scrapeOptions field on this endpoint. "
            f'Move it inside "scrapeOptions": {{"{field}": …}}.'
        )
    return {"field": field, "message": message, "type": err.get("type", "")}


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Unknown fields and schema violations are both 400 INVALID_REQUEST.

    Silent typo acceptance causes long debugging sessions, so the offending
    field is named back to the caller.
    """
    problems = [_problem(err, request.url.path) for err in exc.errors()]
    return JSONResponse(
        status_code=400,
        content={
            "success": False,
            "error": {
                "code": str(ErrorCode.INVALID_REQUEST),
                "message": "Request failed validation",
                "detail": {"problems": problems},
            },
        },
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = getattr(request.state, "trace_id", "unknown")
    # Unhandled errors are logged with a trace id and never leak internals to
    # the caller.
    logger.exception(
        "unhandled_error", trace_id=trace_id, error_type=type(exc).__name__, error=str(exc)
    )
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {
                "code": str(ErrorCode.INTERNAL),
                "message": "An internal error occurred",
                "detail": {"trace_id": trace_id},
            },
        },
    )


# --------------------------------------------------------------------------
# Health and metrics
# --------------------------------------------------------------------------


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, Any]:
    """Liveness, plus whether this instance can actually take work.

    A bare liveness check answered 200 all the way through an outage that lost
    188 of 201 requests, because the process really was alive — it just could
    not accept. Event-loop lag is the part of "healthy" that a caller needs and
    a heartbeat cannot express, so it is reported here.
    """
    # Which rungs actually exist right now. Camoufox's browser build lives in
    # ~/Library/Caches, a cache cleaner deleted it, and the engine dropped
    # `stealth_hard` and `mobile` with one info line — then answered BLOCKED
    # for every DataDome domain for an hour, which reads as the targets
    # refusing us rather than as us having lost the only rungs that pass them
    # (7 Sep 2026). A capability that can vanish has to be reported.
    from engine.api import deps
    from engine.core import search as search_mod

    wired = [str(t) for t in deps.get_fetchers()]
    search_health = search_mod.health_snapshot()

    # Which code this API is running, and whether the workers agree. A worker
    # on an older build drains the queue and fails every job with an internal
    # error while synchronous requests stay perfect — the shape that reads as
    # "the engine is flaky" (9 Sep 2026; see engine/build.py).
    from engine.build import build_id
    from engine.workers.queue import stale_workers

    ours = build_id()
    stale = await stale_workers(ours)

    return {
        "status": "ok",
        "version": __version__,
        "build": ours,
        # Quiet when everything agrees; impossible to miss when it does not.
        "staleWorkers": stale,
        "tiers": wired,
        "deepTiersAvailable": "stealth_hard" in wired,
        # Named rungs only when something is wrong, so a healthy engine stays
        # quiet and a degraded ladder is impossible to miss.
        "searchProvidersDegraded": sorted(n for n, h in search_health.items() if h["tripped"]),
        **loop_monitor.snapshot(),
    }


@app.get("/ready", include_in_schema=False)
async def ready() -> JSONResponse:
    """Readiness. A non-200 pulls the instance from rotation."""
    checks = {"postgres": await db.healthy(), "redis": await _redis_healthy()}
    # Saturation is a readiness failure, not a liveness one: the instance is
    # fine, it just cannot take more right now. Gating here is what lets a
    # caller back off instead of discovering it as a refused connection.
    checks["eventLoop"] = not loop_monitor.saturated
    ok = all(checks.values())
    # Reported, not gated: an API with no worker still serves every synchronous
    # endpoint, so pulling it from rotation would be wrong. But an operator
    # asking "why is nothing processing" should not have to guess.
    from engine.workers.queue import live_workers

    workers = await live_workers()
    return JSONResponse(
        status_code=200 if ok else 503,
        # A refused connection tells a client nothing about when to return, so
        # it guesses — the pilot's client backed off 4/8/12/16s blind. Say it.
        headers={} if ok else {"Retry-After": str(RETRY_AFTER_SECONDS)},
        content={
            "status": "ok" if ok else "degraded",
            "checks": checks,
            "workers": workers,
            **loop_monitor.snapshot(),
        },
    )


async def _redis_healthy() -> bool:
    try:
        client = await get_redis()
        return bool(await client.ping())
    except Exception as exc:  # noqa: BLE001 - readiness must not raise
        logger.warning("redis_unhealthy", error=str(exc))
        return False


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus format. Bound to the internal interface only — never public.

    The deployment binds this behind nginx; see 10-build-plan.md deployment.
    """
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
