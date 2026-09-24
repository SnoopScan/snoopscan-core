"""What a key is allowed to call.

Scopes were stored on every key, offered as four checkboxes in the customer's
dashboard, and enforced NOWHERE. A control that grants nothing and withholds
nothing is worse than no control: it tells a customer they have limited a
key's blast radius when they have not.

How the field does it, measured 9 Sep 2026:

    Typesense  an `actions` list, "which API endpoints the key has access to"
    Algolia    a per-key ACL defining "each allowed feature"
    Stripe     restricted keys, and they recommend AGAINST unrestricted ones
    Firecrawl  no key scoping at all — one key, every endpoint

Firecrawl is the only one that does not scope, and it does not offer the
control either. Offering it and not enforcing it is the one position nobody
holds.

Enforced in `require_api_key`, which every authenticated route already depends
on, rather than per route — a scope check added at each caller is a scope
check somebody forgets on the next endpoint. A path this table does not know
is DENIED, so a new route is unreachable until it is named here; failing open
would make the omission invisible, which is how the first version ended up
enforcing nothing.
"""

from __future__ import annotations

# First path segment after /v1 -> the scope that unlocks it. Sub-paths inherit:
# /v1/crawl/{id}/pages needs `crawl`, because reading a job back is part of
# having run it.
SCOPE_BY_SEGMENT: dict[str, str] = {
    "scrape": "scrape",
    "crawl": "crawl",
    "map": "map",
    "search": "search",
    "extract": "extract",
    "batch": "batch",
    "products": "products",
    "posts": "posts",
    "monitor": "monitor",
    "places": "places",
    # Find Leads rides on `places`: the same capability (businesses by what
    # they do and where), and every existing key that holds one needs the other.
    "leads": "places",
    "parse": "parse",
    "domain": "domain",
    "company": "company",
    # Bought Google results pages. Its own scope so SEO / AI-visibility
    # monitoring can be sold, and granted, apart from plain search.
    "serp": "serp",
    # The template catalogue: a list of field names, no fetch, no charge. It
    # rides on `scrape` because that is what a caller uses them with.
    "templates": "scrape",
    # Not billable endpoints: the unblocker's own fetch and the source probe.
    # They ride on `scrape`, which is the nearest thing a caller would expect
    # to need, rather than being silently exempt.
    "fetch": "scrape",
    "source": "scrape",
}

# Every scope a key can hold. The customer's dashboard offers exactly these.
ALL_SCOPES: tuple[str, ...] = tuple(sorted(set(SCOPE_BY_SEGMENT.values())))


def required_for(path: str) -> str | None:
    """The scope a request to `path` needs, or None if it is not a /v1 route.

    None means "not scoped here" — health, docs, the MCP endpoint and the
    internal routes have their own gates and are not reached through this.
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2 or parts[0] != "v1":
        return None

    return SCOPE_BY_SEGMENT.get(parts[1], _UNKNOWN)


# A sentinel that no key can hold, so an unmapped /v1 route denies rather than
# opens. Naming it in the table is the deliberate act that grants access.
_UNKNOWN = "__unmapped__"
