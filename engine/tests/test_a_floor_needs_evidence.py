"""A tier floor is a claim about a domain. One block is not evidence for it.

`apply_success` has raised the floor only on RAISE_FLOOR_AFTER consecutive
climbs since 0019 — "never straight to `tier`, so a one-off hard climb cannot
pin a whole domain to the expensive end". `apply_block` moved the same floor on
a single event.

Measured 9 Sep 2026 across 4,112 live profiles: 226 of the 347 raised floors
had been raised by exactly ONE block.

    reddit.com   floor=mobile        47 successes, 1 block
                 fetch_log: stealth_hard served it 190 times, mobile 13
    asana.com    floor=impersonate   http succeeded 4 times, never blocked there

What makes one block the wrong bar is that the error is not symmetric. A floor
set too LOW costs one cheap wasted attempt and the request still climbs and
succeeds inside itself. A floor set too HIGH is paid by every later request to
that domain until the 30-day decay.
"""

from __future__ import annotations

from engine.core.fetch.escalation import (
    RAISE_FLOOR_AFTER,
    DomainProfile,
    apply_block,
    apply_success,
)
from engine.core.models import Tier


def _profile(**kw: object) -> DomainProfile:
    return DomainProfile("example.test", **kw)  # type: ignore[arg-type]


def test_one_block_does_not_move_the_floor() -> None:
    """The whole bug, in one assertion."""
    profile = _profile()

    apply_block(profile, Tier.HTTP)

    assert profile.min_working_tier == Tier.HTTP, "one block is one event, not evidence"
    assert profile.block_count == 1, "it is still counted"
    assert profile.blocks_at_floor == 1


def test_a_run_of_blocks_at_the_floor_does_move_it() -> None:
    profile = _profile()

    for _ in range(RAISE_FLOOR_AFTER):
        apply_block(profile, Tier.HTTP)

    assert profile.min_working_tier == Tier.IMPERSONATE
    assert profile.blocks_at_floor == 0, "the streak resets once it has been acted on"


def test_a_success_ends_the_run() -> None:
    """A probabilistic block is exactly a run that a success interrupts.

    pranx.com refused at every rung on 7 Sep and answered first try on the 9th.
    Under the old rule that first refusal cost it a permanently dearer floor.
    """
    profile = _profile()

    apply_block(profile, Tier.HTTP)
    apply_block(profile, Tier.HTTP)
    apply_success(profile, Tier.HTTP, content_length=4000)
    apply_block(profile, Tier.HTTP)
    apply_block(profile, Tier.HTTP)

    assert profile.min_working_tier == Tier.HTTP, "two, then two, is not a run of three"
    assert profile.blocks_at_floor == 2


def test_a_block_above_the_floor_says_nothing_about_the_floor() -> None:
    """The rung that failed is not the rung we start at. Counting it would let
    a doomed climb at the top of the ladder condemn the bottom of it."""
    profile = _profile(min_working_tier=Tier.HTTP)

    for _ in range(RAISE_FLOOR_AFTER * 2):
        apply_block(profile, Tier.STEALTH)

    assert profile.min_working_tier == Tier.HTTP
    assert profile.blocks_at_floor == 0
    assert profile.block_count == RAISE_FLOOR_AFTER * 2, "still counted for the breaker"


def test_the_vendor_is_still_recorded_from_the_first_block() -> None:
    """Raising the floor needs evidence; NAMING the WAF does not — one
    Cloudflare challenge is a fact about the domain whatever else follows."""
    profile = _profile()

    apply_block(profile, Tier.HTTP, vendor="cloudflare")

    assert profile.detected_waf == "cloudflare"
    assert profile.min_working_tier == Tier.HTTP


def test_the_two_paths_that_move_the_floor_use_the_same_bar() -> None:
    """The fault was that they did not: one asked for three, the other for one.

    Kept as a test rather than a comment because the next person to tune one
    constant should be told the other exists.
    """
    up_by_blocks = _profile()
    for _ in range(RAISE_FLOOR_AFTER):
        apply_block(up_by_blocks, Tier.HTTP)

    up_by_climbs = _profile()
    for _ in range(RAISE_FLOOR_AFTER):
        apply_success(up_by_climbs, Tier.IMPERSONATE, content_length=4000)

    assert up_by_blocks.min_working_tier == Tier.IMPERSONATE
    assert up_by_climbs.min_working_tier == Tier.IMPERSONATE


def test_a_forced_rung_is_no_evidence_for_raising_the_floor() -> None:
    """A day of forced-rung benchmarks put example.com's floor at `browser`
    (22 Sep 2026): every later request paid five credits for a page the plain
    rung serves. What the caller asked for says nothing about what the domain
    needs."""
    profile = DomainProfile("example.com")
    for _ in range(RAISE_FLOOR_AFTER * 3):
        apply_success(profile, Tier.STEALTH_HARD, 1200, chosen=False)
    assert profile.min_working_tier == Tier.HTTP
    assert profile.success_count == RAISE_FLOOR_AFTER * 3


def test_a_forced_cheaper_rung_that_works_still_lowers_the_floor() -> None:
    profile = DomainProfile("example.com", min_working_tier=Tier.BROWSER)
    apply_success(profile, Tier.HTTP, 1200, chosen=False)
    assert profile.min_working_tier == Tier.HTTP


def test_the_engine_s_own_climbs_still_raise_it() -> None:
    profile = DomainProfile("hard.example")
    for _ in range(RAISE_FLOOR_AFTER):
        apply_success(profile, Tier.BROWSER, 1200)
    assert profile.min_working_tier != Tier.HTTP
