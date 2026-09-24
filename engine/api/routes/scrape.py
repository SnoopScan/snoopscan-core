"""POST /v1/scrape — fetch and extract a single URL. Synchronous."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from engine.api import billing
from engine.api.deps import ApiKeyDep, ServiceDep
from engine.api.js_gate import guard_js_execution
from engine.core.models import ScrapeRequest

router = APIRouter(tags=["scrape"])


@router.post("/scrape")
async def scrape(
    body: ScrapeRequest,
    key: ApiKeyDep,
    service: ServiceDep,
) -> dict[str, Any]:
    guard_js_execution(body, key.allow_js_exec)
    billing.assert_credits(key)

    options = body.model_copy()
    # Amazon publishes no JSON twin; its product is read off the page, so the
    # page has to come back. Requested here, removed again below if the caller
    # did not ask for it.
    wants_html = _needs_page_html(body.url)
    added_raw = wants_html and not options._has_format("rawHtml")
    if added_raw:
        options = options.model_copy(update={"formats": [*options.formats, "rawHtml"]})
    outcome = await service.scrape(
        body.url, options, plan_concurrency=key.concurrency, owner_ref=key.owner_ref
    )
    page_html = outcome.data.rawHtml if wants_html else None
    await _attach_product(
        outcome,
        body.url,
        service,
        html=page_html,
        escalate=options.escalate,
        plan_concurrency=key.concurrency,
        owner_ref=key.owner_ref,
    )
    if added_raw:
        outcome.data.rawHtml = None
    await billing.charge(key, endpoint="scrape", url=body.url, cost=outcome.data.cost)

    return {
        "success": True,
        "data": outcome.data.model_dump(by_alias=True, exclude_none=False),
    }


def _needs_page_html(url: str) -> bool:
    try:
        from engine.platforms.service import PlatformService
    except ImportError:
        return False
    try:
        return bool(PlatformService.wants_page_html(url))
    except Exception:  # noqa: BLE001
        return False


async def _attach_product(
    outcome: Any,
    url: str,
    service: Any,
    html: str | None = None,
    escalate: bool = True,
    plan_concurrency: int | None = None,
    owner_ref: str | None = None,
) -> None:
    """A Shopify or WooCommerce PRODUCT page has a JSON twin the store publishes
    — price, variants, stock, images — so the markdown comes with the product
    beside it. One extra tier-0 request, billed as one platform page. The
    module is proprietary; without it this is a no-op.

    Amazon has no JSON twin: its product is read off the page. Amazon also
    withholds the price from the honest tier-0 client, so when the page came
    back without one and the caller left escalation on, this climbs ONCE to the
    browser tier — the tier Amazon does show a price to — and re-reads. That is
    the same principle as the block ladder (climb on evidence the tier fell
    short), extended from "no content" to "the content is incomplete for what
    this page is". It fires only for Amazon, only when the price is missing, and
    only below the browser tier, so an ordinary page never pays for it.
    """
    platform = outcome.data.metadata.platform
    if platform not in ("shopify", "woocommerce", "amazon"):
        return
    path = url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url
    if platform != "amazon" and not ("/products/" in f"/{path}" or "/product/" in f"/{path}"):
        return
    try:
        from engine.platforms.detect import Platform
        from engine.platforms.service import PlatformService
    except ImportError:
        return
    platforms = PlatformService(service)
    try:
        product = await platforms.product_for_page(url, Platform(platform), html)
    except Exception:  # noqa: BLE001 - a bonus that fails is not a failed scrape
        return

    if product is not None and platform == "amazon" and product.price is None and escalate:
        product = (
            await _amazon_price_climb(outcome, url, service, platforms, plan_concurrency, owner_ref)
            or product
        )

    if product is not None:
        outcome.data.product = product.model_dump(exclude_none=False)
        if platform != "amazon":  # Amazon's came off the page already paid for
            extras = dict(outcome.data.cost.extras)
            extras["platform_page"] = extras.get("platform_page", 0) + 1
            outcome.data.cost = outcome.data.cost.model_copy(update={"extras": extras})


async def _amazon_price_climb(
    outcome: Any,
    url: str,
    service: Any,
    platforms: Any,
    plan_concurrency: int | None = None,
    owner_ref: str | None = None,
) -> Any | None:
    """Re-fetch an Amazon product page at the browser tier for the price the
    honest tier is not shown. Only climbs when we are below browser already."""
    from engine.core.models import TIER_ORDER, ScrapeOptions, Tier
    from engine.platforms.detect import Platform

    current = outcome.data.cost.tier
    if current and TIER_ORDER.index(Tier(current)) >= TIER_ORDER.index(Tier.BROWSER):
        return None  # already as high as this buys us; Amazon simply withheld it
    try:
        climbed = await service.scrape(
            url,
            ScrapeOptions(formats=["rawHtml"], maxAge=0, tier="browser"),
            plan_concurrency=plan_concurrency,
            owner_ref=owner_ref,
        )
    except Exception:  # noqa: BLE001 - the price is a bonus, not the scrape
        return None
    product = await platforms.product_for_page(url, Platform.AMAZON, climbed.data.rawHtml)
    if product is None or product.price is None:
        return None
    # The browser fetch is real spend and is billed as what it was.
    outcome.data.cost = climbed.data.cost.model_copy(update={"cached": outcome.data.cost.cached})
    return product
