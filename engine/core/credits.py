"""Credits: what a successful response costs a customer.

The rules mirror the app's CreditPricer exactly and are read from the
credit_costs table so an operator can change a price without a deploy:

    direct 1 · proxied 2 · browser-class 5 · PDF +1 per page
    your own cache hit 0 · another account's cached row 1

Failed requests are never charged: callers pass successes only.
"""

from __future__ import annotations

from engine.core.models import Cost

BROWSER_TIERS = frozenset({"browser", "stealth", "stealth_hard", "mobile"})

DEFAULT_COSTS: dict[str, int] = {
    "direct": 1,
    "proxied": 2,
    "browser": 5,
    "pdf_page": 1,
    # Your own row, re-read inside the window: free. We already billed you for
    # the fetch and charging twice for one piece of work is indefensible.
    "cached": 0,
    # SOMEBODY ELSE'S row. Instant to serve, but it is not work this customer
    # paid for, and at zero the more popular a URL is the less we earn on it —
    # which is backwards. Priced at the `direct` rate: 80% off a browser page,
    # so "cache hits are cheap" survives as a claim while the free ride ends.
    # Firecrawl charges 1 credit for EVERY cached page, own or not, on the
    # same 48-hour default window.
    "cached_shared": 1,
    # Not fetches: priced flat, per call, additive to any fetch in the same event.
    "search": 2,
    # A search answered by a rung we PAY for. The free rungs cost us nothing and
    # bill 2; a bought Google result costs real money per call, and charging the
    # same for both is a straight loss that grows with traffic. The number is a
    # starting point and is meant to be tuned from the desk, not from here.
    "search_paid": 10,
    # A bought Google results page (/v1/serp), and the AI Overview on it, which
    # doubles the provider's price for that call. Starting points, tuned from
    # the desk like search_paid.
    "serp": 10,
    "serp_ai_overview": 10,
    "map": 1,
    # Off-page intelligence: RDAP, DNS and our own link graph. Priced as one
    # unit rather than as a fetch, because none of it touches the target's web
    # server — there is no tier, no proxy and no browser underneath it.
    "domain": 1,
    # Company enrichment: the homepage plus up to four contact/about pages, all
    # at tier 0/1 (a company that needs a browser to answer is not worth
    # enriching). Flat, because the caller cannot predict how many candidate
    # pages a site has, and ~5 cheap fetches is what a direct-rate page-each
    # crawl would have cost anyway.
    "company": 3,
    "model_extract": 5,
    # Places (12-places-source.md): one results page of ~20 businesses, and one
    # detail panel per business. Browser fetches underneath, priced as units the
    # caller can predict rather than as raw fetch tiers. Starting points; tuned
    # from the desk like every other key.
    "places_search": 5,
    "places_detail": 2,
    # Find Leads: charged per business DELIVERED, never per fetch behind it.
    # A lead is a business with its phone, website and address; contacts are
    # what its own site publishes (emails, socials, a contact form), charged
    # only when found; a person is a named contact found for a role asked for.
    "lead": 2,
    "lead_contacts": 1,
    "lead_person": 2,
    # Platform shortcuts: one listing page of a store's or blog's own API —
    # up to 250 products or 100 posts per request. A tier-0 fetch underneath,
    # priced as the unit the caller sees.
    "platform_page": 1,
}


def key_for(cost: Cost) -> str:
    if cost.cached:
        return "cached" if cost.cache_own else "cached_shared"
    if cost.tier in BROWSER_TIERS:
        return "browser"
    return "proxied" if cost.proxy_bytes > 0 else "direct"


# The owner every operator key belongs to. Operator work is OUR work — the
# unblocker's fetches, MCP sessions, the desk's own tools — so it is metered
# and visible like anyone else's, but never refused for want of credits: an
# internal batch cut off mid-run is an outage, not a saving. Before this
# existed such keys simply had no owner, which meant `charge` returned early
# and the spend appeared nowhere at all.
OPERATOR_OWNER = "operator"


def credits_for(cost: Cost, table: dict[str, int] | None = None) -> int:
    """Price a cost. The operator's table LAYERS over the defaults.

    It must not replace them. The desk's table is whatever was saved the last
    time someone looked at that screen, so a key added since — a new billable
    rung, a new endpoint — is simply absent from it. With a plain `t.get(k, 0)`
    that absence prices as FREE, and the feature ships giving itself away until
    somebody happens to read the ledger.

    An operator who genuinely wants something free sets it to 0 explicitly.
    Silence means "not configured", and the safe reading of that is the default.
    """
    t = {**DEFAULT_COSTS, **(table or {})}
    fetched = cost.tier is not None or cost.cached
    base = t.get(key_for(cost), 0) if fetched else 0
    pdf = 0 if cost.cached else cost.pdf_pages * t.get("pdf_page", 0)
    extras = sum(n * t.get(k, 0) for k, n in cost.extras.items())
    return base + pdf + extras
