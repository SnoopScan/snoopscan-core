"""Credit rules — the same cases the app's CreditPricer test asserts (parity)."""

from __future__ import annotations

from engine.core.credits import DEFAULT_COSTS, credits_for, key_for
from engine.core.models import Cost

CASES = [
    (Cost(tier="http", proxy_bytes=0), 1),
    (Cost(tier="impersonate", proxy_bytes=0), 1),
    (Cost(tier="impersonate", proxy_bytes=4096, proxy_used=True), 2),
    (Cost(tier="browser"), 5),
    (Cost(tier="stealth_hard", proxy_bytes=9000, proxy_used=True), 5),
    (Cost.from_cache(cache_own=True), 0),
    (Cost.from_cache(), 1),  # unowned defaults to charged, not free
    (Cost(tier="http", pdf_pages=12), 13),
    # Your own row, re-read: still free, PDF surcharge included.
    (Cost(tier="browser", cached=True, cache_own=True, pdf_pages=12), 0),
    # Somebody else's row: a cache hit, but not work this customer paid for.
    (Cost(tier="browser", cached=True, cache_own=False, pdf_pages=12), 1),
]


def test_locked_rules() -> None:
    for cost, expected in CASES:
        assert credits_for(cost) == expected, (cost, expected)


def test_follows_an_edited_table_not_a_constant() -> None:
    table = dict(DEFAULT_COSTS, browser=8)
    assert credits_for(Cost(tier="browser"), table) == 8


def test_key_for_classes() -> None:
    assert key_for(Cost(tier="mobile")) == "browser"
    assert key_for(Cost(tier="http", proxy_bytes=1)) == "proxied"
    assert key_for(Cost(tier="http")) == "direct"
    assert key_for(Cost.from_cache(cache_own=True)) == "cached"
    # The default is NOT own: an unowned row — stored before attribution, or
    # by a monitor with no owner — must charge rather than give away.
    assert key_for(Cost.from_cache()) == "cached_shared"


def test_a_cache_hit_never_costs_more_than_fetching_it_fresh() -> None:
    """Whatever the table says, reading a stored row must not out-price doing
    the work — otherwise the cache is a penalty and callers set maxAge=0."""
    from engine.core.credits import DEFAULT_COSTS

    for tier, fresh_key in (("http", "direct"), ("stealth_hard", "browser")):
        fresh = credits_for(Cost(tier=tier))
        shared = credits_for(Cost(tier=tier, cached=True))
        own = credits_for(Cost(tier=tier, cached=True, cache_own=True))
        assert own <= shared <= fresh, (tier, own, shared, fresh)
        assert fresh == DEFAULT_COSTS[fresh_key]


def test_the_shared_rate_is_configurable_from_the_desk() -> None:
    """Like every other price. An operator who wants the old free-for-all sets
    it to 0 explicitly; silence still means the default."""
    cost = Cost(tier="stealth_hard", cached=True)

    assert credits_for(cost, {"cached_shared": 0}) == 0
    assert credits_for(cost, {"cached_shared": 3}) == 3
    assert credits_for(cost, {}) == 1, "an absent key must fall back to the default, not to free"
