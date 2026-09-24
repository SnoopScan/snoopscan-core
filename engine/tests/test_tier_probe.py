"""A raised tier floor comes back down by probing, not by waiting a week."""

from __future__ import annotations

from engine.core.fetch.escalation import (
    PROBE_EVERY,
    DomainProfile,
    apply_block,
    apply_success,
    starting_tier,
)
from engine.core.models import Tier


def _raised(successes: int) -> DomainProfile:
    p = DomainProfile("shop.test")
    p.min_working_tier = Tier.BROWSER
    p.success_count = successes
    return p


def test_every_nth_success_probes_one_rung_down() -> None:
    assert starting_tier(_raised(PROBE_EVERY)) == Tier.IMPERSONATE
    assert starting_tier(_raised(2 * PROBE_EVERY)) == Tier.IMPERSONATE


def test_other_requests_start_at_the_floor() -> None:
    assert starting_tier(_raised(PROBE_EVERY - 1)) == Tier.BROWSER
    assert starting_tier(_raised(0)) == Tier.BROWSER, "a fresh raise is not probed at once"


def test_a_floor_at_tier_0_has_nowhere_to_probe() -> None:
    p = DomainProfile("easy.test")
    p.success_count = PROBE_EVERY
    assert starting_tier(p) == Tier.HTTP


def test_a_forced_tier_is_never_probed() -> None:
    assert starting_tier(_raised(PROBE_EVERY), forced=Tier.BROWSER) == Tier.BROWSER


def test_a_successful_probe_lowers_the_floor() -> None:
    p = _raised(PROBE_EVERY)
    apply_success(p, Tier.IMPERSONATE, 5_000)
    assert p.min_working_tier == Tier.IMPERSONATE


def test_a_failed_probe_does_not_raise_the_floor() -> None:
    """apply_block only raises when the blocked tier IS the floor."""
    p = _raised(PROBE_EVERY)
    apply_block(p, Tier.IMPERSONATE)
    assert p.min_working_tier == Tier.BROWSER
