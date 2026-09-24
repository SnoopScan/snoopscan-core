"""Escalation controller — table-driven, no network.

Given a sequence of tier verdicts, the controller must escalate, stop, or
return correctly, and must never exceed the total time budget.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from engine.core.detect import validator
from engine.core.detect.validator import Reason
from engine.core.fetch.base import MIN_TIER_TIME_MS, FetchRequest, FetchResult
from engine.core.fetch.escalation import (
    PROBE_EVERY,
    DomainProfile,
    EscalationController,
    apply_block,
    apply_success,
    budget_for_tier,
    decay_profile,
    ladder_from,
    should_open_circuit,
    starting_tier,
)
from engine.core.models import Tier
from engine.settings import settings


@dataclass
class ScriptedFetcher:
    """Returns a canned response, so the controller is tested in isolation."""

    name: str
    status: int | None = 200
    body: bytes = b"<html><body>" + b"real content " * 200 + b"</body></html>"
    headers: dict[str, str] | None = None
    error: str | None = None
    latency_ms: int = 100
    calls: int = 0

    async def fetch(self, req: FetchRequest) -> FetchResult:
        self.calls += 1
        return FetchResult(
            url=req.url,
            status_code=self.status,
            headers=self.headers or {},
            body=self.body,
            content_type="text/html",
            tier=self.name,
            latency_ms=self.latency_ms,
            bytes_transferred=len(self.body),
            error=self.error,
        )

    async def healthcheck(self) -> bool:
        return True


def request(timeout_ms: int = 60_000) -> FetchRequest:
    return FetchRequest(url="https://example.com/page", timeout_ms=timeout_ms)


# --------------------------------------------------------------------------
# Ladder construction
# --------------------------------------------------------------------------


def test_ladder_starts_where_the_domain_left_off() -> None:
    ladder = ladder_from(Tier.BROWSER)
    assert ladder[0] == Tier.BROWSER
    assert Tier.HTTP not in ladder


def test_ladder_restricted_to_built_tiers() -> None:
    ladder = ladder_from(Tier.HTTP, available={Tier.HTTP, Tier.IMPERSONATE})
    assert ladder == [Tier.HTTP, Tier.IMPERSONATE]


def test_starting_tier_uses_domain_memory() -> None:
    profile = DomainProfile(domain="hard.example.com", min_working_tier=Tier.BROWSER)
    assert starting_tier(profile) == Tier.BROWSER


def test_detected_waf_skips_a_tier_known_to_fail() -> None:
    """DataDome means go straight to the Camoufox tier rather than wasting an
    attempt on Patchright.

    Skipped in the open core: which tier clears which vendor is part of the
    withheld knowledge base, so the mapping this asserts does not exist there.
    """
    pytest.importorskip("engine.knowledge.waf_tiers")
    profile = DomainProfile(domain="x.com", min_working_tier=Tier.HTTP, detected_waf="datadome")
    assert starting_tier(profile) == Tier.STEALTH_HARD


def test_forced_tier_overrides_memory() -> None:
    profile = DomainProfile(domain="x.com", min_working_tier=Tier.BROWSER)
    assert starting_tier(profile, forced=Tier.HTTP) == Tier.HTTP


def test_actions_force_browser_tier() -> None:
    profile = DomainProfile(domain="x.com", min_working_tier=Tier.HTTP)
    assert starting_tier(profile, floor=Tier.BROWSER) == Tier.BROWSER


# --------------------------------------------------------------------------
# Escalation behaviour
# --------------------------------------------------------------------------


async def test_success_at_first_tier_does_not_escalate() -> None:
    tier0 = ScriptedFetcher(name="http")
    tier1 = ScriptedFetcher(name="impersonate")
    controller = EscalationController({Tier.HTTP: tier0, Tier.IMPERSONATE: tier1})

    outcome = await controller.fetch(request(), DomainProfile("example.com"))

    assert outcome.succeeded
    assert outcome.tiers_attempted == ["http"]
    assert tier1.calls == 0


async def test_block_at_tier0_escalates_to_tier1() -> None:
    tier0 = ScriptedFetcher(name="http", status=403)
    tier1 = ScriptedFetcher(name="impersonate")
    controller = EscalationController({Tier.HTTP: tier0, Tier.IMPERSONATE: tier1})

    outcome = await controller.fetch(request(), DomainProfile("example.com"))

    assert outcome.succeeded
    assert outcome.tiers_attempted == ["http", "impersonate"]


async def test_404_does_not_escalate() -> None:
    """The expensive mistake this prevents: climbing the ladder to fetch a page
    that does not exist."""
    tier0 = ScriptedFetcher(name="http", status=404, body=b"<html>Not Found</html>")
    tier1 = ScriptedFetcher(name="impersonate")
    controller = EscalationController({Tier.HTTP: tier0, Tier.IMPERSONATE: tier1})

    outcome = await controller.fetch(request(), DomainProfile("example.com"))

    assert not outcome.succeeded
    assert outcome.verdict.reason == Reason.TARGET_ERROR
    assert tier1.calls == 0, "must not escalate on a genuine 404"


async def test_blocked_at_every_tier_returns_blocked() -> None:
    tier0 = ScriptedFetcher(name="http", status=403)
    tier1 = ScriptedFetcher(name="impersonate", status=403)
    controller = EscalationController({Tier.HTTP: tier0, Tier.IMPERSONATE: tier1})

    outcome = await controller.fetch(request(), DomainProfile("example.com"))

    assert not outcome.succeeded
    assert outcome.verdict.reason == Reason.BLOCKED
    assert outcome.tiers_attempted == ["http", "impersonate"]


async def test_transport_error_retries_same_tier_before_escalating() -> None:
    """A flaky connection retried at tier 3 costs a hundred times more and
    fixes nothing."""
    tier0 = ScriptedFetcher(name="http", status=None, error="timeout: connection")
    tier1 = ScriptedFetcher(name="impersonate")
    controller = EscalationController({Tier.HTTP: tier0, Tier.IMPERSONATE: tier1})

    outcome = await controller.fetch(request(), DomainProfile("example.com"))

    assert tier0.calls == 2, "tier 0 should be retried once before escalating"
    assert outcome.tiers_attempted == ["http", "http", "impersonate"]


async def test_escalate_false_tries_only_one_tier() -> None:
    tier0 = ScriptedFetcher(name="http", status=403)
    tier1 = ScriptedFetcher(name="impersonate")
    controller = EscalationController({Tier.HTTP: tier0, Tier.IMPERSONATE: tier1})

    outcome = await controller.fetch(request(), DomainProfile("example.com"), escalate=False)

    assert tier1.calls == 0
    assert outcome.tiers_attempted == ["http"]


async def test_circuit_open_short_circuits_without_fetching() -> None:
    import time

    tier0 = ScriptedFetcher(name="http")
    controller = EscalationController({Tier.HTTP: tier0})
    profile = DomainProfile("example.com", circuit_open_until=time.time() + 300)

    outcome = await controller.fetch(request(), profile)

    assert not outcome.succeeded
    assert outcome.verdict.signal == "circuit_open"
    assert tier0.calls == 0


async def test_budget_exhaustion_stops_escalating_but_still_tries_once() -> None:
    """If the remaining budget is below the next tier's minimum viable time,
    stop escalating — starting an attempt that will time out costs money for
    nothing. The FIRST attempt always runs, whatever the caller's timeout.
    """
    tier0 = ScriptedFetcher(name="http", status=403, latency_ms=100)
    tier1 = ScriptedFetcher(name="impersonate", status=403)
    controller = EscalationController({Tier.HTTP: tier0, Tier.IMPERSONATE: tier1})

    outcome = await controller.fetch(request(timeout_ms=1_500), DomainProfile("example.com"))

    assert not outcome.succeeded
    assert tier0.calls == 1, "the first tier is always attempted"
    assert tier1.calls == 0, "must not escalate without budget for the next tier"
    assert outcome.verdict.signal == "deadline_exceeded"


async def test_budget_exhaustion_keeps_what_the_last_rung_learned() -> None:
    """Running out of time must not erase the vendor the previous rung saw.

    g2.com: a real DataDome challenge at the cheap rung, budget gone before
    the rung that could pass it, recorded with vendor=None — so the profile
    never got the floor and the next request repeated the whole climb.
    """
    challenge = (
        b"<html><body><script>var dd={'rt':'c','host':'geo.captcha-delivery.com'}"
        b"</script></body></html>"
    )
    tier0 = ScriptedFetcher(
        name="http", status=403, body=challenge, headers={"x-datadome": "protected"}, latency_ms=100
    )
    tier1 = ScriptedFetcher(name="impersonate", status=403)
    controller = EscalationController({Tier.HTTP: tier0, Tier.IMPERSONATE: tier1})

    outcome = await controller.fetch(request(timeout_ms=1_500), DomainProfile("g2.example"))

    assert outcome.verdict.signal == "deadline_exceeded"
    assert outcome.verdict.vendor == "datadome", "the vendor the real rung saw must survive"
    assert outcome.verdict.details["last_signal"] == "datadome"


# --------------------------------------------------------------------------
# Budget division
# --------------------------------------------------------------------------


def test_cheap_tiers_are_capped_by_the_setting() -> None:
    # Read from settings, not a literal: 15s was the number this test froze
    # while the ladder was three rungs deep in practice.
    cap = settings.tier0_max_ms
    assert budget_for_tier(Tier.HTTP, 60_000) == cap
    assert budget_for_tier(Tier.IMPERSONATE, 60_000) == cap
    assert budget_for_tier(Tier.HTTP, 1_000) == 1_000


def test_a_rung_leaves_every_later_rung_its_minimum() -> None:
    later = (
        MIN_TIER_TIME_MS[Tier.STEALTH]
        + MIN_TIER_TIME_MS[Tier.STEALTH_HARD]
        + MIN_TIER_TIME_MS[Tier.MOBILE]
    )
    assert budget_for_tier(Tier.BROWSER, 78_000) == 78_000 - later
    assert budget_for_tier(Tier.STEALTH_HARD, 40_000) == 40_000 - MIN_TIER_TIME_MS[Tier.MOBILE]


def test_a_rung_never_gets_less_than_its_own_minimum() -> None:
    assert budget_for_tier(Tier.STEALTH, 5_000) == MIN_TIER_TIME_MS[Tier.STEALTH]
    assert budget_for_tier(Tier.MOBILE, 1_000) == MIN_TIER_TIME_MS[Tier.MOBILE]


# --------------------------------------------------------------------------
# Profile updates
# --------------------------------------------------------------------------


def test_success_at_lower_tier_lowers_the_floor() -> None:
    profile = DomainProfile("example.com", min_working_tier=Tier.BROWSER)
    apply_success(profile, Tier.IMPERSONATE, 5_000)
    assert profile.min_working_tier == Tier.IMPERSONATE


def test_blocks_at_the_floor_raise_it() -> None:
    """CHANGED 9 Sep 2026 — this asserted that ONE block raises the floor.

    It was the contract, and the contract was wrong: 226 of 347 raised floors
    on the live instance had been raised by a single block, reddit.com among
    them, pinned to `mobile` on one refusal out of 48 attempts. The bar is now
    the same `RAISE_FLOOR_AFTER` the success path has always used. The WAF name
    is still taken from the first block — naming a vendor is a fact, not a
    claim about which rung works. See test_a_floor_needs_evidence.py.
    """
    from engine.core.fetch.escalation import RAISE_FLOOR_AFTER

    profile = DomainProfile("example.com", min_working_tier=Tier.HTTP)

    apply_block(profile, Tier.HTTP, vendor="cloudflare")
    assert profile.min_working_tier == Tier.HTTP, "one block is not evidence"
    assert profile.detected_waf == "cloudflare", "but the vendor is known now"

    for _ in range(RAISE_FLOOR_AFTER - 1):
        apply_block(profile, Tier.HTTP)
    assert profile.min_working_tier == Tier.IMPERSONATE


def test_running_baseline_converges() -> None:
    profile = DomainProfile("example.com")
    for length in (5000, 5200, 4800, 5100, 4900):
        apply_success(profile, Tier.HTTP, length)
    assert profile.avg_content_length is not None
    assert 4900 <= profile.avg_content_length <= 5100
    assert profile.stdev_content_length is not None
    assert profile.stdev_content_length > 0


def test_decay_lowers_tier_on_a_quiet_domain() -> None:
    """Without decay, a domain that dropped its WAF costs browser-tier money
    for ever."""
    profile = DomainProfile("example.com", min_working_tier=Tier.BROWSER)
    decay_profile(profile, days_since_last_block=45)
    assert profile.min_working_tier == Tier.IMPERSONATE


def test_decay_does_nothing_on_a_recently_blocked_domain() -> None:
    profile = DomainProfile("example.com", min_working_tier=Tier.BROWSER)
    decay_profile(profile, days_since_last_block=2)
    assert profile.min_working_tier == Tier.BROWSER


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ([True] * 20, False),
        ([False] * 20, True),
        # 11 of 20 failed — over the 50% threshold.
        ([False] * 11 + [True] * 9, True),
        # Exactly half is not "over" the threshold.
        ([False] * 10 + [True] * 10, False),
        # Too few samples to judge.
        ([False] * 5, False),
    ],
)
def test_circuit_breaker_threshold(outcomes: list[bool], expected: bool) -> None:
    assert should_open_circuit(outcomes) is expected


def test_a_domain_that_has_only_ever_been_blocked_can_still_probe_down() -> None:
    """The probe exists to un-stick a wrong floor, and excluded the domains
    most likely to have one.

    `and profile.success_count` made zero falsy, so a profile with nothing but
    blocks never probed: 125 domains sat on a raised floor with no WAF vendor
    recorded, taxed to the 5-credit rung by a verdict about their content.
    """
    stuck = DomainProfile("only-ever-blocked.example")
    stuck.min_working_tier = Tier.STEALTH
    stuck.success_count = 0
    stuck.block_count = PROBE_EVERY  # its Nth request

    assert starting_tier(stuck) == Tier.BROWSER, "a probe must start one rung lower"


def test_the_probe_does_not_fire_on_every_request() -> None:
    # One cheap request in PROBE_EVERY, not one per request: a genuinely hard
    # domain must not pay a failed probe every single time.
    profile = DomainProfile("hard.example")
    profile.min_working_tier = Tier.STEALTH
    profile.success_count = 0
    profile.block_count = PROBE_EVERY + 1

    assert starting_tier(profile) == Tier.STEALTH


def test_a_floor_of_http_has_nothing_to_probe() -> None:
    profile = DomainProfile("easy.example")
    profile.success_count = 0
    profile.block_count = 0
    assert starting_tier(profile) == Tier.HTTP


# --------------------------------------------------------------------------
# A wall nothing clears
# --------------------------------------------------------------------------


GOOGLE_WALL = (
    b"<html><body>Our systems have detected unusual traffic from your "
    b"computer network.</body></html>"
)


async def test_a_terminal_wall_stops_the_climb_instead_of_buying_every_rung() -> None:
    """Google's results page answers this from every rung we have and from a
    residential exit in the country asked for (measured 20 Sep 2026). Climbing
    spends the caller's credits five more times to be told the same thing, and
    fills the failure window so the breaker shuts the whole host behind it.
    """
    # Needs the WAF signature list, which the open core does not ship: there
    # the page is correctly unrecognised, so this assertion cannot hold.
    if validator._signature_path() is None:
        pytest.skip("needs the block signature list (engine/knowledge)")
    fetchers = {
        tier: ScriptedFetcher(name=str(tier), status=200, body=GOOGLE_WALL) for tier in Tier
    }
    controller = EscalationController(fetchers)
    profile = DomainProfile(domain="google.com")

    outcome = await controller.fetch(request(), profile)

    assert not outcome.verdict.ok
    assert outcome.verdict.signal == "google_unusual_traffic"
    assert len(outcome.attempts) == 1, (
        f"it climbed {len(outcome.attempts)} rungs on a wall none of them clear"
    )
    assert sum(f.calls for f in fetchers.values()) == 1


async def test_an_ordinary_block_still_climbs() -> None:
    """The guard above must not turn every block into a single attempt: a
    stopped climb on a wall a higher rung WOULD have cleared is the more
    expensive mistake of the two."""
    fetchers = {tier: ScriptedFetcher(name=str(tier), status=403, body=b"no") for tier in Tier}
    controller = EscalationController(fetchers)

    outcome = await controller.fetch(request(), DomainProfile(domain="ordinary.example.com"))

    assert not outcome.verdict.ok
    assert len(outcome.attempts) > 1, "an ordinary refusal is still worth a higher rung"


async def test_an_attempt_records_which_url_it_was_for() -> None:
    """Without this the fetch log only ever said "something on this domain
    failed", which is enough to open the breaker and never enough to tell a
    hostile path from the rest of the site."""
    fetchers = {tier: ScriptedFetcher(name=str(tier)) for tier in Tier}
    controller = EscalationController(fetchers)

    outcome = await controller.fetch(request(), DomainProfile(domain="example.com"))

    assert outcome.attempts
    assert all(a.url == "https://example.com/page" for a in outcome.attempts)


# --------------------------------------------------------------------------
# maxTier is a spending limit, so it has to actually limit spending
# --------------------------------------------------------------------------


def test_a_ceiling_below_the_start_yields_no_ladder_rather_than_the_start() -> None:
    """`tiers[:1]` kept the STARTING tier when the ceiling was below it, so a
    caller who set maxTier as a cost cap was billed past it silently."""
    assert ladder_from(Tier.STEALTH_HARD, max_tier=Tier.IMPERSONATE) == []


def test_a_ceiling_at_or_above_the_start_still_caps_the_ladder() -> None:
    ladder = ladder_from(Tier.HTTP, max_tier=Tier.BROWSER)
    assert ladder == [Tier.HTTP, Tier.IMPERSONATE, Tier.BROWSER]
    assert Tier.STEALTH not in ladder


async def test_the_caller_is_told_their_own_ceiling_stopped_it() -> None:
    """And told apart from "we have no such rung", which is our configuration
    problem and a different fix entirely."""
    fetchers = {tier: ScriptedFetcher(name=str(tier)) for tier in Tier}
    controller = EscalationController(fetchers)
    profile = DomainProfile(domain="hard.example.com", min_working_tier=Tier.STEALTH_HARD)

    outcome = await controller.fetch(request(), profile, max_tier=Tier.IMPERSONATE)

    assert outcome.verdict.signal == "tier_ceiling_below_floor"
    assert outcome.verdict.details["max_tier"] == str(Tier.IMPERSONATE)
    assert not outcome.attempts, "nothing may be attempted, and nothing charged"
    assert sum(f.calls for f in fetchers.values()) == 0


async def test_a_missing_rung_is_still_reported_as_ours() -> None:
    controller = EscalationController({})  # nothing built at all
    outcome = await controller.fetch(request(), DomainProfile(domain="x.example.com"))
    assert outcome.verdict.signal == "no_tier_available"
