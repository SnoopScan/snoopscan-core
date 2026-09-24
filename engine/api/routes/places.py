"""POST /v1/places/search — business listings from Google Maps. Synchronous.

The contract is public; the source behind it is not. `engine.places` sits on
the proprietary side of the split (it needs the browser tiers, the proxy layer
and leadgen), so it is imported here at call time and its absence is a 503
with a reason — the same shape as a search ladder with no rungs, and for the
same reason: nothing is broken, this deployment simply does not have it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import ValidationError

from engine.api import billing
from engine.api.deps import ApiKeyDep, ServiceDep
from engine.core.errors import InvalidRequest, PlacesUnavailable
from engine.core.models import Cost

router = APIRouter(tags=["places"])


def _load_service(scrape_service: Any) -> Any:
    """The Places service, or PlacesUnavailable on the open core."""
    try:
        from engine.places.models import PlacesSearchRequest  # noqa: F401 - presence check
        from engine.places.service import PlacesService
    except ImportError as exc:
        raise PlacesUnavailable() from exc
    return PlacesService(scrape_service)


def _validated(body: dict[str, Any]) -> Any:
    """The request model, or a 400 that names the field.

    The body arrives as a plain dict because the model lives on the proprietary
    side, so FastAPI never validates it and a pydantic error raised in here is
    not a `RequestValidationError` — it fell through to the 500 handler. Same
    envelope as the app-wide one, so a caller cannot tell the two doors apart.
    """
    from engine.places.models import PlacesSearchRequest

    try:
        return PlacesSearchRequest.model_validate(body)
    except ValidationError as exc:
        problems = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ())),
                "message": err.get("msg", "invalid"),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        raise InvalidRequest("Request failed validation", {"problems": problems}) from exc


@router.post("/places/search")
async def places_search(
    body: dict[str, Any], key: ApiKeyDep, service: ServiceDep
) -> dict[str, Any]:
    places = _load_service(service)
    from engine.places.service import search_url

    req = _validated(body)
    billing.assert_credits(key)

    data = await places.search(req)

    cost = Cost(
        tier="browser",
        extras={"places_search": data.search_pages, "places_detail": data.detail_fetches},
    )
    await billing.charge(key, endpoint="places", url=search_url(req.query, req.location), cost=cost)
    data.cost = cost.model_dump(exclude_none=True)
    return {"success": True, "data": data.model_dump(exclude_none=False)}
