"""The tier floor must be able to rise, not only fall.

`apply_success` only ever lowered `min_working_tier`, and only a hard block
raised it. A domain whose cheap rungs return 200 with a nav shell — thin, not
blocked — therefore kept a floor it could never satisfy and bought a doomed
attempt on every request. ancestry.com: floor `stealth`, 199 successes, every
one served by `stealth_hard` (measured 7 Sep 2026).
"""

from __future__ import annotations

from engine.core.fetch.escalation import (
    RAISE_FLOOR_AFTER,
    DomainProfile,
    apply_success,
)
from engine.core.models import Tier


def _profile(floor: Tier) -> DomainProfile:
    return DomainProfile(domain="ancestry.com", min_working_tier=floor)


def test_repeated_success_above_the_floor_raises_it() -> None:
    p = _profile(Tier.STEALTH)
    for _ in range(RAISE_FLOOR_AFTER):
        apply_success(p, Tier.STEALTH_HARD, 50_000)
    assert p.min_working_tier == Tier.STEALTH_HARD
    assert p.climbs_above_floor == 0, "counter resets once the floor has moved"


def test_one_hard_climb_does_not_pin_the_domain() -> None:
    """A single expensive success is exactly the wrong thing to write into
    shared state — it is how one bad call taxes a whole host."""
    p = _profile(Tier.STEALTH)
    apply_success(p, Tier.STEALTH_HARD, 50_000)
    assert p.min_working_tier == Tier.STEALTH
    assert p.climbs_above_floor == 1


def test_a_success_at_the_floor_resets_the_run() -> None:
    p = _profile(Tier.STEALTH)
    apply_success(p, Tier.STEALTH_HARD, 50_000)
    apply_success(p, Tier.STEALTH_HARD, 50_000)
    apply_success(p, Tier.STEALTH, 50_000)  # the floor works after all
    assert p.climbs_above_floor == 0
    assert p.min_working_tier == Tier.STEALTH
    apply_success(p, Tier.STEALTH_HARD, 50_000)
    assert p.min_working_tier == Tier.STEALTH, "the run must start over"


def test_a_cheaper_success_still_lowers_the_floor_immediately() -> None:
    """Unchanged behaviour: one cheap success is proof, and being wrong about
    it costs one wasted cheap request."""
    p = _profile(Tier.STEALTH_HARD)
    apply_success(p, Tier.HTTP, 50_000)
    assert p.min_working_tier == Tier.HTTP
    assert p.climbs_above_floor == 0


def test_the_floor_converges_but_keeps_a_rung_of_headroom() -> None:
    """It must converge in one run of evidence, not one rung per run —
    ancestry.co.uk pays ~60s for the rungs beneath `mobile`, so fifteen
    requests of crawling upward is twelve minutes of waste. But it stops one
    rung BELOW the serving tier, so a site that gets easier is still caught."""
    p = _profile(Tier.HTTP)
    for _ in range(RAISE_FLOOR_AFTER):
        apply_success(p, Tier.STEALTH_HARD, 50_000)
    assert p.min_working_tier == Tier.STEALTH, "one below the tier that served"


def test_an_adjacent_climb_lands_exactly_on_the_serving_tier() -> None:
    """With only one rung between, `one below the server` would not move at
    all — the floor must still rise."""
    p = _profile(Tier.STEALTH)
    for _ in range(RAISE_FLOOR_AFTER):
        apply_success(p, Tier.STEALTH_HARD, 50_000)
    assert p.min_working_tier == Tier.STEALTH_HARD


def test_the_floor_never_runs_off_the_top() -> None:
    p = _profile(Tier.MOBILE if hasattr(Tier, "MOBILE") else Tier.STEALTH_HARD)
    top = p.min_working_tier
    for _ in range(RAISE_FLOOR_AFTER * 3):
        apply_success(p, top, 50_000)
    assert p.min_working_tier == top
