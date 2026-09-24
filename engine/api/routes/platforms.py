"""POST /v1/products and POST /v1/posts — a site's own data, the way it
publishes it. Synchronous.

A Shopify store lists its whole catalogue at /products.json; WooCommerce's Store
API, WordPress's REST API, Substack's archive, Squarespace's ?format=json and
Discourse's /latest.json all answer without a key. Asking is one request per
page instead of a crawl. The module that knows this is proprietary; its absence
is a 503 with a reason, like Places.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from engine.api import billing
from engine.api.deps import ApiKeyDep, ServiceDep
from engine.core.errors import PlatformsUnavailable
from engine.core.models import Cost
from engine.core.ssrf import resolve_and_validate

router = APIRouter(tags=["platforms"])


class ListingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        description=(
            "The site to read. Its own public catalogue or feed is used where the platform has one."
        )
    )
    limit: int = Field(
        default=500, ge=1, le=10_000, description="The most items to return, 1 to 10,000."
    )


def _load(scrape_service: Any) -> Any:
    try:
        from engine.platforms.service import PlatformService
    except ImportError as exc:
        raise PlatformsUnavailable() from exc
    return PlatformService(scrape_service)


async def _run(kind: str, body: ListingRequest, key: Any, service: Any) -> dict[str, Any]:
    await resolve_and_validate(body.url)
    billing.assert_credits(key)
    platforms = _load(service)
    listing = await (platforms.products if kind == "products" else platforms.posts)(
        body.url, limit=body.limit
    )
    cost = Cost(tier="http", extras={"platform_page": listing.pages_fetched})
    await billing.charge(key, endpoint=kind, url=body.url, cost=cost)
    data = listing.model_dump(exclude_none=False)
    data["cost"] = cost.model_dump(exclude_none=True)
    return {"success": True, "data": data}


@router.post("/products")
async def products(body: ListingRequest, key: ApiKeyDep, service: ServiceDep) -> dict[str, Any]:
    """Every product a store publishes, from its own catalogue endpoint."""
    return await _run("products", body, key, service)


@router.post("/posts")
async def posts(body: ListingRequest, key: ApiKeyDep, service: ServiceDep) -> dict[str, Any]:
    """Every post a site publishes, from its API or, failing that, its feed."""
    return await _run("posts", body, key, service)
