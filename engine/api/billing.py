"""Charge a key for one successful response. Used by the synchronous routes.

Two halves: `assert_credits` before any fetch (a customer with nothing left
gets a 402 and nothing is fetched); `charge` after success (event, rollup,
balance in one transaction). A failed request is never charged.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from engine.core import credits as rules
from engine.core.errors import InsufficientCredits, Unauthorized
from engine.core.models import Cost
from engine.storage import repositories as repo
from engine.storage.repositories import ApiKey

_cost_table: dict[str, int] | None = None


async def cost_table() -> dict[str, int]:
    """The operator's table, read once per process and refreshed by /internal writes."""
    global _cost_table
    if _cost_table is None:
        _cost_table = await repo.get_credit_costs() or dict(rules.DEFAULT_COSTS)
    return _cost_table


def invalidate_cost_table() -> None:
    global _cost_table
    _cost_table = None


def assert_credits(key: ApiKey) -> None:
    # Operator work is never REFUSED for want of credits — an internal batch
    # stopped mid-run is an outage. It is still metered; see `charge`.
    if key.owner_ref == rules.OPERATOR_OWNER:
        return
    if key.suspended:
        raise Unauthorized("Account suspended")
    if key.credits_remaining <= 0:
        raise InsufficientCredits(key.credits_remaining)


async def page_price(*, proxied: bool = False) -> int:
    """What one ordinary page costs, for quoting work BEFORE it is done.

    The cheapest honest rung: a direct fetch, or a proxied one when the caller
    has asked for a proxy. A page that escalates to a browser costs more, so
    this is a floor, not a promise — which is the only thing anyone can quote
    in advance, because the rung a site forces is not knowable until it is
    tried.
    """
    return rules.credits_for(Cost(tier="http", proxy_bytes=1 if proxied else 0), await cost_table())


async def affordable_limit(key: ApiKey, requested: int, *, per_page: int) -> int:
    """The page limit this key can actually pay for.

    A queued crawl bills per page as it runs, so a caller could previously
    submit 10,000 pages against a balance of 5 and have the job accepted, only
    to die part-done with no warning at submission. The limit that fits is
    allowed as asked; one that does not is lowered to what the balance covers;
    a balance covering no pages at all is refused here, before anything is
    queued, rather than discovered halfway through.

    Operator work is never lowered — it is metered but not gated, the same
    split `assert_credits` makes.
    """
    if key.owner_ref == rules.OPERATOR_OWNER or per_page <= 0:
        return requested
    affordable = key.credits_remaining // per_page
    if affordable <= 0:
        raise InsufficientCredits(key.credits_remaining)
    return min(requested, affordable)


async def charge(
    key: ApiKey, *, endpoint: str, url: str | None, cost: Cost, job_id: str | None = None
) -> int:
    # Everything meters, operator keys included. This used to return early for
    # a key with no owner, which is how a week of real fetching — real proxy
    # bandwidth, really paid for — left no trace in usage_events at all.
    owner = key.owner_ref
    if owner is None:
        # require_api_key refuses these at the door, so this is unreachable
        # over HTTP. It stays for the direct callers (the crawler and monitor
        # hold a key object), where silently not charging is the old bug.
        raise Unauthorized("This key has no owner and cannot be metered")
    amount = rules.credits_for(cost, await cost_table())
    cost.credits = amount
    host = urlsplit(url).hostname if url else None
    return await repo.record_usage(
        key.id,
        owner_ref=owner,
        endpoint=endpoint,
        host=host,
        url=url,
        tier=cost.tier,
        proxy_bytes=cost.proxy_bytes,
        cached=cost.cached,
        cache_own=cost.cache_own,
        pdf_pages=cost.pdf_pages,
        credits=amount,
        job_id=job_id,
    )
