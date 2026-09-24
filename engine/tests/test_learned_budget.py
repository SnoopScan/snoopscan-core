"""A domain that has proved it needs longer than the default gets longer.

The engine already learns which tier a domain answers on, whether it needs a
proxy, which country it serves and which WAF sits in front of it. It did not
learn TIME, and time is what decides whether any of the rest gets a chance.

etsy.com: the floor is already correct — `detected_waf: datadome` starts it at
`stealth_hard`, so the ladder is [stealth_hard, mobile] — but one deep attempt
costs 15-50s and a challenge there is a coin toss. Inside the 90s default only
two or three attempts fit, so a domain that would answer on the fourth returns
BLOCKED (measured 7 Sep 2026).

Raising the global default would spend that time on every domain, including the
sixteen hundred that answer in under a second.
"""

from __future__ import annotations

import pytest

from engine.core.fetch.escalation import (
    BUDGET_CEILING_MS,
    BUDGET_MIN_SAMPLES,
    DomainProfile,
    budget_for_domain,
    record_success_time,
)

DEFAULT = 90_000


def _slow_domain(each_ms: int = 150_000, samples: int = BUDGET_MIN_SAMPLES) -> DomainProfile:
    p = DomainProfile(domain="etsy.com")
    for _ in range(samples):
        record_success_time(p, each_ms)
    return p


def test_a_slow_domain_is_granted_more_than_the_default() -> None:
    granted = budget_for_domain(_slow_domain(), DEFAULT, caller_set=False)
    assert granted > DEFAULT


def test_a_fast_domain_is_left_alone() -> None:
    """The 1,600 domains that answer in a second must not pay for etsy."""
    p = DomainProfile(domain="example.com")
    for _ in range(10):
        record_success_time(p, 800)
    assert budget_for_domain(p, DEFAULT, caller_set=False) == DEFAULT


def test_one_slow_request_is_not_enough() -> None:
    """A single slow fetch is weather, not climate."""
    p = _slow_domain(samples=BUDGET_MIN_SAMPLES - 1)
    assert budget_for_domain(p, DEFAULT, caller_set=False) == DEFAULT


def test_a_caller_who_names_a_timeout_gets_exactly_it() -> None:
    """An explicit timeout is a promise, not a hint — including when the
    learned value is far larger."""
    assert budget_for_domain(_slow_domain(), 30_000, caller_set=True) == 30_000


def test_nothing_learned_exceeds_the_ceiling() -> None:
    """A domain that wants five minutes is one to fix, not to wait for."""
    p = _slow_domain(each_ms=BUDGET_CEILING_MS * 3)
    assert budget_for_domain(p, DEFAULT, caller_set=False) == BUDGET_CEILING_MS


def test_an_unknown_domain_gets_the_default() -> None:
    assert (
        budget_for_domain(DomainProfile(domain="new.example"), DEFAULT, caller_set=False) == DEFAULT
    )


def test_the_running_mean_tracks_the_observations() -> None:
    p = DomainProfile(domain="x.example")
    for ms in (60_000, 72_000, 66_000):
        record_success_time(p, ms)
    assert p.timed_success_count == 3
    assert p.avg_success_ms == pytest.approx(66_000, abs=1_000)
    assert p.stdev_success_ms > 0


def test_a_variable_domain_gets_headroom_above_its_mean() -> None:
    """Two deviations, so ordinary spread is covered without one pathological
    fetch setting the budget for every later request."""
    steady = DomainProfile(domain="steady.example")
    jumpy = DomainProfile(domain="jumpy.example")
    for ms in (100_000, 100_000, 100_000):
        record_success_time(steady, ms)
    for ms in (40_000, 100_000, 160_000):
        record_success_time(jumpy, ms)
    assert budget_for_domain(jumpy, DEFAULT, caller_set=False) > budget_for_domain(
        steady, DEFAULT, caller_set=False
    )
