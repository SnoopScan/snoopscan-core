"""POST /v1/extract and POST /v1/search.

Both complete the contract in 01-api-surface.md, and both are endpoints
Firecrawl exposes — interface compatibility is the point, so anything already
written against their API migrates by changing a base URL.

The behavioural guarantee on `/v1/extract` is ours and is stated in the
response: output is VALIDATED against the supplied JSON Schema before it is
returned. A value that does not conform is an error, not a passthrough. That
is the difference between "the model said something" and "we extracted data".
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter

from engine.api import billing
from engine.api.deps import ApiKeyDep, ServiceDep
from engine.api.js_gate import guard_js_execution
from engine.core import search as serp
from engine.core.errors import EngineError, ErrorCode, SearchUnavailableError
from engine.core.extract.classify import parse_json_ld
from engine.core.extract.model import Spec, model_caller
from engine.core.extract.structured_json import extract_against_schema
from engine.core.models import Cost, ExtractRequest, SearchRequest
from engine.core.search import SearchUnavailable
from engine.core.ssrf import resolve_and_validate

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["extract"])


@router.post("/extract")
async def extract_structured(
    body: ExtractRequest, key: ApiKeyDep, service: ServiceDep
) -> dict[str, Any]:
    """Schema-constrained extraction across one or more URLs.

    Per-URL failure does not fail the request: the response is an array of
    {url, data, confidence, error}, so a page that genuinely lacks the fields
    is reported as such rather than filled with invented values.
    """

    guard_js_execution(body, key.allow_js_exec)
    billing.assert_credits(key)
    byok = body.model is not None
    try:
        model = (
            model_caller(Spec(body.model.provider, body.model.apiKey, body.model.name))
            if body.model is not None
            else model_caller()
        )
    except Exception as exc:  # noqa: BLE001 - a bad key or SDK is the caller's problem, reported plainly
        raise EngineError(
            ErrorCode.INVALID_REQUEST, f"The model could not be set up: {exc}"
        ) from exc
    results: list[dict[str, Any]] = []

    # HTML is required, not optional: JSON-LD lives in a <script> tag and is
    # the free path that answers most schemas without a model call. Reading it
    # off `data.html` means the caller's format choice would silently decide
    # whether structured markup was consulted at all — the same trap as link
    # screening needing the source anchors.
    options = body.scrapeOptions.model_copy()
    if not options._has_format("html"):
        options.formats = [*options.formats, "html"]

    for url in body.urls:
        try:
            await resolve_and_validate(url)
            outcome = await service.scrape(
                url, options, plan_concurrency=key.concurrency, owner_ref=key.owner_ref
            )
        except EngineError as exc:
            results.append({"url": url, "data": None, "confidence": 0.0, "error": exc.message})
            continue

        # Structured markup first — free, and often answers the schema
        # outright. Only reach for a model when it does not.
        hints: dict[str, Any] | None = None
        blocks = _json_ld_from(outcome)
        if blocks:
            hints = blocks[0] if len(blocks) == 1 else {"@graph": blocks}

        if model is None:
            extracted = extract_against_schema(
                markdown=outcome.data.markdown or "",
                structured_hints=hints,
                schema=body.effective_schema,
                prompt=body.prompt,
            )
        else:
            # The model call is synchronous; a worker thread keeps the loop free.
            extracted = await asyncio.to_thread(
                extract_against_schema,
                outcome.data.markdown or "",
                hints,
                body.effective_schema,
                body.prompt,
                model,
            )

        # One event per URL: the fetch at its tier, plus the model when it ran.
        cost = outcome.data.cost.model_copy()
        if extracted.source == "model" and not byok:
            cost.extras = {**cost.extras, "model_extract": 1}
        await billing.charge(key, endpoint="extract", url=url, cost=cost)

        results.append(
            {
                "url": url,
                "data": extracted.data,
                "confidence": extracted.confidence,
                "source": extracted.source if extracted.data is not None else None,
                "error": extracted.error,
                "validation_errors": extracted.validation_errors,
            }
        )

    return {"success": True, "data": results}


def _json_ld_from(outcome: Any) -> list[dict[str, Any]]:
    """Pull JSON-LD off the fetched page, when html was requested."""
    html = getattr(outcome.data, "html", None)
    if not html:
        return []
    from selectolax.parser import HTMLParser

    return parse_json_ld(HTMLParser(html))


@router.post("/search")
async def search(body: SearchRequest, key: ApiKeyDep, service: ServiceDep) -> dict[str, Any]:
    """Web search, optionally with page bodies.

    `scrapeOptions` turns each result into a full page fetch, which is a
    fan-out — `limit` is enforced hard for that reason.
    """

    guard_js_execution(body, key.allow_js_exec)
    billing.assert_credits(key)
    query = serp.SearchQuery(
        query=body.query,
        limit=body.limit,
        country=body.location.country if body.location else None,
        place=body.place,
        language=body.language,
        device=body.device,
        freshness=body.freshness,
        safe_search=body.safeSearch,
        page=body.page,
        auto_correct=body.autoCorrect,
    )

    try:
        # The ladder, not one vendor: a provider that has started refusing us
        # costs the caller a little latency instead of the whole endpoint. A
        # provider that cannot honour what was asked is dropped from it.
        answer = await serp.search(query)
    except SearchUnavailable as exc:
        # 503, never an empty list: an agent told "no results" concludes the
        # thing does not exist, which is a different and worse answer.
        raise SearchUnavailableError(str(exc)) from exc

    payload: list[dict[str, Any]] = [item.to_payload() for item in answer.results]
    # Billed on the rung that answered. A free rung must not subsidise a bought
    # one, and a caller whose query the free rungs could serve must not pay for
    # Google they never used.
    await billing.charge(
        key,
        endpoint="search",
        url=None,
        cost=Cost(extras={"search_paid" if answer.paid else "search": 1}),
    )

    if body.scrapeOptions is not None:
        for entry in payload:
            try:
                outcome = await service.scrape(
                    entry["url"], body.scrapeOptions, plan_concurrency=key.concurrency
                )
            except EngineError as exc:
                entry["error"] = exc.message
                continue
            entry["markdown"] = outcome.data.markdown
            entry["metadata"] = outcome.data.metadata.model_dump()
            await billing.charge(key, endpoint="search", url=entry["url"], cost=outcome.data.cost)

    return {
        "success": True,
        "data": {
            "results": payload,
            "provider": answer.provider,
            "relatedSearches": answer.related,
        },
    }
