"""A circuit breaker has to see slow failure, and stop re-learning fast.

Measured 9 Sep 2026 on the three domains with zero lifetime successes:

    wisdomlib.org   542 attempts over 33h · 70 min fetch time · 14.7 MB · 0 ok
    hamariweb.com    76 attempts over 30h
    namexray.com     22 attempts over  9h

The breaker never opened for any of them. Its window was "attempts in the
last five minutes" with a twenty-sample minimum, and wisdomlib peaked at 19.
And when it DID open elsewhere it stayed open a flat fifteen minutes, then let
a full-ladder retry through — sixteen top-rung re-learnings an hour, for ever,
for a domain that has never worked.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

import pytest

from engine.core.detect.validator import Reason, Verdict
from engine.core.fetch.escalation import (
    DomainProfile,
    apply_success,
    circuit_backoff_minutes,
    should_open_circuit,
)
from engine.core.models import Tier
from engine.settings import settings


def _async(value: Any) -> Any:
    """A stand-in for a repo coroutine that just answers."""

    async def _answer(*_: object, **__: object) -> Any:
        return value

    return _answer


def _blocked() -> Verdict:
    """An ordinary block, the kind that counts toward the breaker."""
    return Verdict(ok=False, reason=Reason.BLOCKED, signal="status_403", confidence=1.0)


def test_the_backoff_doubles_and_caps() -> None:
    base, cap = settings.circuit_open_minutes, settings.circuit_open_max_minutes

    assert circuit_backoff_minutes(0) == base
    assert circuit_backoff_minutes(1) == base
    assert circuit_backoff_minutes(2) == base * 2
    assert circuit_backoff_minutes(3) == base * 4
    assert circuit_backoff_minutes(4) == base * 8
    assert circuit_backoff_minutes(7) == base * 64
    assert circuit_backoff_minutes(8) == cap, "eight in a row is a day"
    assert circuit_backoff_minutes(500) == cap, "and it never overflows past it"


def test_a_domain_that_never_works_ends_up_probed_once_a_day() -> None:
    """The point of the whole change, as a number: one top-rung retry a day
    instead of sixteen an hour."""
    per_day_before = (24 * 60) // settings.circuit_open_minutes
    per_day_after = (24 * 60) // circuit_backoff_minutes(10)

    assert per_day_before == 96
    assert per_day_after == 1


def test_a_success_puts_the_breaker_back_on_the_short_fuse() -> None:
    """The backoff is for domains that never work. One that just did is not
    one of them, and must not carry a day-long fuse into its next hiccup."""
    profile = DomainProfile("example.test", circuit_opens=6)

    apply_success(profile, Tier.HTTP, content_length=4000)

    assert profile.circuit_opens == 0


def test_the_window_is_about_attempts_not_the_clock() -> None:
    """`should_open_circuit` is pure and was never the fault — it judges
    whatever it is handed. The fault was what it was handed: a five-minute
    slice that never held twenty polite failures. That bound lives in the
    query, and this pins it to a day."""
    import inspect

    from engine.storage import repositories

    src = inspect.getsource(repositories.recent_domain_outcomes)
    assert "interval '24 hours'" in src
    assert "interval '5 minutes'" not in src

    # And the judge itself: twenty straight failures open it, nineteen do not.
    assert should_open_circuit([False] * 20) is True
    assert should_open_circuit([False] * 19) is False


@pytest.mark.anyio
async def test_the_refusal_states_the_real_remaining_time() -> None:
    """With backoff the breaker can hold a day. A message quoting the
    15-minute base would be a lie the caller acts on."""
    from engine.core.fetch.base import FetchRequest
    from engine.core.fetch.escalation import EscalationController

    profile = DomainProfile(
        "example.test",
        circuit_open_until=time.time() + 6 * 3600,  # six hours from now
        circuit_opens=5,
    )
    controller = EscalationController({})
    outcome = await controller.fetch(FetchRequest(url="https://example.test/"), profile)

    assert outcome.verdict.signal == "circuit_open"
    remaining = outcome.verdict.details["retry_after_s"]
    assert 6 * 3600 - 5 <= remaining <= 6 * 3600
    assert outcome.verdict.details["consecutive_opens"] == 5


def test_the_error_the_caller_sees_uses_that_time() -> None:
    from engine.core.detect.validator import Reason, Verdict
    from engine.core.scrape_service import _error_for

    verdict = Verdict(
        ok=False,
        reason=Reason.BLOCKED,
        signal="circuit_open",
        confidence=1.0,
        details={"domain": "example.test", "retry_after_s": 6 * 3600, "consecutive_opens": 5},
    )
    err = _error_for(verdict, [])

    assert "360 minutes" in err.message, err.message
    assert "15 minutes" not in err.message


def test_a_deadline_exceeded_error_does_not_carry_the_circuit_breakers_minutes() -> None:
    """Reported live: a caller's own 60-second `timeout` tripped
    deadline_exceeded, and the error's detail carried "minutes": 15 —
    settings.circuit_open_minutes, attached to every engine-side signal
    regardless of whether it had anything to do with the breaker. Read next
    to a message that says "raise timeout", it looks like a 15-minute
    cooldown the caller should wait out instead."""
    from engine.core.detect.validator import Reason, Verdict
    from engine.core.scrape_service import _error_for

    verdict = Verdict(
        ok=False,
        reason=Reason.BLOCKED,
        signal="deadline_exceeded",
        confidence=1.0,
        details={"remaining_ms": 0, "tier": "impersonate"},
    )
    err = _error_for(verdict, ["http", "impersonate"])

    assert "minutes" not in err.detail
    assert "raise" in err.message.lower() or "timeout" in err.message.lower()


@pytest.mark.anyio
@pytest.mark.parametrize("signal", ["circuit_open", "url_backoff_open"])
async def test_a_request_the_breaker_refused_is_not_evidence_about_the_domain(
    monkeypatch, signal: str
) -> None:
    """Measured live on hamariweb.com, 9 Sep 2026: the breaker tripped at the
    16th request, and the nine refused requests that followed each counted
    themselves as another opening — `circuit_opens` went to 10 and the hold
    to the 24-hour cap in ten seconds. The refusals never touched the site.

    The url's own backoff is the same kind of refusal. Missed here, every
    visitor refused during it re-armed it for a fresh fifteen minutes, so a
    page people kept trying never came back (22 Sep 2026)."""
    from engine.core import url_backoff
    from engine.core.detect.validator import Reason, Verdict
    from engine.core.scrape_service import ScrapeService
    from engine.storage import repositories as repo

    saved: list[DomainProfile] = []

    async def _recent(domain: str) -> list[bool]:
        return [False] * 20  # still full of the same failures

    async def _save(profile: DomainProfile, **kw: object) -> None:
        saved.append(profile)

    monkeypatch.setattr(repo, "recent_domain_outcomes", _recent)
    monkeypatch.setattr(repo, "save_domain_profile", _save)
    rearmed: list[object] = []

    async def _note(*a: object) -> None:
        rearmed.append(a)

    monkeypatch.setattr(url_backoff, "note_failure", _note)

    profile = DomainProfile(
        "example.test",
        circuit_opens=1,
        circuit_open_until=time.time() + 900,
        failure_count=3,
        block_count=3,
    )
    before = (
        profile.circuit_opens,
        profile.circuit_open_until,
        profile.failure_count,
        profile.block_count,
    )
    refusal = Verdict(
        ok=False,
        reason=Reason.BLOCKED,
        signal=signal,
        confidence=1.0,
        details={"domain": "example.test", "retry_after_s": 900},
    )

    service = ScrapeService({}, persist=True)
    await service._record_failure(profile, refusal, [], "example.test")

    after = (
        profile.circuit_opens,
        profile.circuit_open_until,
        profile.failure_count,
        profile.block_count,
    )
    assert after == before, f"a refusal changed the profile: {before} -> {after}"
    assert saved == [], "and nothing was written"
    assert rearmed == [], "and the url's backoff was not re-armed"


# --------------------------------------------------------------------------
# One hostile path must not bury the rest of the site
# --------------------------------------------------------------------------


async def test_a_url_that_works_is_probed_while_the_domain_breaker_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The breaker is keyed on the DOMAIN, which protects us from hammering a
    host and, left alone, hands the caller a site-wide outage caused by one
    page. Google's results page fills the failure window by itself and
    everything else on google.com is refused behind it (measured 20 Sep 2026).

    A url with a success of its own in the window gets a probe instead.
    """
    from engine.core import scrape_service as svc
    from engine.core.models import ScrapeOptions

    seen: list[DomainProfile] = []

    class Recorder:
        async def fetch(self, req: object, profile: DomainProfile, **kw: object) -> None:
            seen.append(profile)
            raise RuntimeError("stop here: the profile is what this test is about")

    open_profile = DomainProfile("google.com", circuit_open_until=time.time() + 900)
    monkeypatch.setattr(svc.repo, "load_domain_profile", _async(open_profile))
    monkeypatch.setattr(svc.repo, "url_worked_recently", _async(True))

    service = svc.ScrapeService({}, persist=True)
    service._controller = Recorder()  # type: ignore[assignment]
    with contextlib.suppress(Exception):
        await service.scrape("https://google.com/a-page-that-reads-fine", ScrapeOptions())

    assert seen, "the fetch was never attempted"
    assert not seen[0].circuit_open, "a url with its own success was refused anyway"


async def test_an_unknown_url_on_a_failing_domain_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe is bounded by evidence. Without that bound the breaker stops
    protecting anything: every fresh url would walk straight past it."""
    from engine.core import scrape_service as svc
    from engine.core.models import ScrapeOptions

    seen: list[DomainProfile] = []

    class Recorder:
        async def fetch(self, req: object, profile: DomainProfile, **kw: object) -> None:
            seen.append(profile)
            raise RuntimeError("stop here")

    open_profile = DomainProfile("google.com", circuit_open_until=time.time() + 900)
    monkeypatch.setattr(svc.repo, "load_domain_profile", _async(open_profile))
    monkeypatch.setattr(svc.repo, "url_worked_recently", _async(False))

    service = svc.ScrapeService({}, persist=True)
    service._controller = Recorder()  # type: ignore[assignment]
    with contextlib.suppress(Exception):
        await service.scrape("https://google.com/never-seen-before", ScrapeOptions())

    assert seen, "the fetch was never attempted"
    assert seen[0].circuit_open, "an unknown path walked past the breaker"


async def test_one_url_failing_backs_off_that_url_not_the_whole_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fault this exists for. Google's results page fills the failure
    window on its own; shutting google.com for it refused the privacy policy
    too (ENGINE_REFUSED, measured live 20 Sep 2026)."""
    from engine.core import scrape_service as svc
    from engine.core import url_backoff

    same_url = b"\x01" * 32
    window = [(same_url, False)] * settings.circuit_window
    backed_off: list[tuple[str, bytes, int]] = []

    monkeypatch.setattr(svc.repo, "recent_domain_outcomes_by_url", _async(window))
    monkeypatch.setattr(svc.repo, "save_domain_profile", _async(None))

    async def _note(domain: str, digest: bytes, minutes: int) -> None:
        backed_off.append((domain, digest, minutes))

    monkeypatch.setattr(url_backoff, "note_failure", _note)

    profile = DomainProfile("google.com")
    service = svc.ScrapeService({}, persist=True)
    await service._record_failure(profile, _blocked(), [], "google.com")

    assert backed_off, "the url was not backed off"
    assert backed_off[0][1] == same_url
    assert profile.circuit_open_until is None, "the whole domain was shut for one page"
    assert profile.circuit_opens == 0


async def test_failures_spread_across_a_host_still_shut_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The breaker still has to do its job: 542 attempts at a domain with no
    lifetime successes is the case it was built for."""
    from engine.core import scrape_service as svc
    from engine.core import url_backoff

    window = [(bytes([i]) * 32, False) for i in range(settings.circuit_window)]
    monkeypatch.setattr(svc.repo, "recent_domain_outcomes_by_url", _async(window))
    monkeypatch.setattr(svc.repo, "save_domain_profile", _async(None))
    monkeypatch.setattr(url_backoff, "note_failure", _async(None))

    profile = DomainProfile("dead.example.com")
    service = svc.ScrapeService({}, persist=True)
    await service._record_failure(profile, _blocked(), [], "dead.example.com")

    assert profile.circuit_open_until is not None, "a host failing everywhere stayed open"
    assert profile.circuit_opens == 1


async def test_a_backed_off_url_does_not_claim_the_whole_site_is_off_limits() -> None:
    """The message is the product here. Telling someone their domain is
    blocked when one page is sends them chasing the wrong cause — the exact
    mistake `circuit_open` already cost a colleague an afternoon for."""
    from engine.core.errors import EngineRefused

    page = EngineRefused("url_backoff_open", tiers_attempted=[], minutes=15)
    site = EngineRefused("circuit_open", tiers_attempted=[], minutes=15)

    assert "URL" in page.message and "rest of the site is unaffected" in page.message
    assert "domain" not in page.message, "a one-page refusal must not talk about the domain"
    assert "domain" in site.message, "and a real domain refusal still says domain"


async def test_a_url_refusal_is_reported_separately_from_a_domain_one() -> None:
    """Same refusal path, different signal, so the two are told apart in the
    answer and in the logs."""
    fetchers: dict[Tier, object] = {}
    controller = __import__(
        "engine.core.fetch.escalation", fromlist=["EscalationController"]
    ).EscalationController(fetchers)
    from engine.core.fetch.base import FetchRequest

    req = FetchRequest(url="https://google.com/search?q=x", timeout_ms=1000)

    one_page = DomainProfile(
        "google.com", circuit_open_until=time.time() + 900, circuit_is_url_only=True
    )
    whole_site = DomainProfile("google.com", circuit_open_until=time.time() + 900)

    assert (await controller.fetch(req, one_page)).verdict.signal == "url_backoff_open"
    assert (await controller.fetch(req, whole_site)).verdict.signal == "circuit_open"
