"""Usage rolls up per COST KEY, so the breakdown always sums to the total.

`usage_daily` has four tier columns and the engine bills twelve keys, so
search, map, extract, places, PDF pages and the platform shortcuts never
landed in a bucket. The members "By request type" table priced four buckets
against a total that included all twelve and came to less, and the customer
could not account for the difference on their own usage screen.

Seven more columns would have fixed today and broken again the next time a
price was added from the desk, which needs no deploy. A row per key needs no
migration at all.
"""

from __future__ import annotations

from engine.core.credits import DEFAULT_COSTS, credits_for, key_for
from engine.core.models import Cost


def test_every_billable_shape_maps_to_a_key_that_can_be_rolled_up() -> None:
    """A cost that priced above zero and produced no key would vanish from
    the breakdown while still being charged."""
    shapes = [
        Cost(tier="http"),
        Cost(tier="http", proxy_bytes=2048),
        Cost(tier="stealth_hard"),
        Cost(tier="http", cached=True, cache_own=True),
        Cost(tier="http", cached=True),
    ]

    for cost in shapes:
        key = key_for(cost)
        assert key in DEFAULT_COSTS, f"{cost.tier} priced to an unknown key {key!r}"


def test_the_foreign_cache_hit_has_its_own_bucket() -> None:
    """The rollup used to derive its own tier name and had never heard of
    `cached_shared`, so every foreign hit was counted as a free one."""
    assert key_for(Cost(tier="stealth_hard", cached=True)) == "cached_shared"
    assert key_for(Cost(tier="stealth_hard", cached=True, cache_own=True)) == "cached"
    assert credits_for(Cost(tier="stealth_hard", cached=True)) == 1


def test_the_rollup_uses_the_pricer_rather_than_its_own_copy() -> None:
    """Five copies of the tier-to-key rule existed; the rollup's was one, and
    it drifted. If this import disappears, the copy is back."""
    import inspect

    from engine.storage import repositories

    src = inspect.getsource(repositories.record_usage)
    assert "key_for(" in src, "the rollup is deriving the cost key itself again"
    assert "BROWSER_CLASS" not in src, "the rollup kept its own tier list"


def test_a_new_desk_key_needs_no_migration() -> None:
    """The point of the table: a key the operator adds prices and rolls up
    with no schema change. Proven by pricing a key the defaults do not have."""
    cost = Cost(tier="http", extras={"a_new_thing": 2})

    assert credits_for(cost, {"a_new_thing": 7}) == DEFAULT_COSTS["direct"] + 14
