"""Escalation controller (03-fetch-tiers.md section 6).

Owns the tier sequence and the time budget. Implements principle P1: start at
the cheapest tier a domain has historically needed, escalate only on evidence,
and never escalate on a genuine 404 — a browser will not conjure a page that
does not exist, it will just cost a hundred times more to not find it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from engine.core.detect.validator import DomainStats, Reason, Verdict, validate
from engine.core.fetch.base import MIN_TIER_TIME_MS, Fetcher, FetchRequest, FetchResult
from engine.core.models import TIER_ORDER, Tier
from engine.settings import settings

logger = structlog.get_logger(__name__)


@dataclass
class DomainProfile:
    """The subset of domain_profiles the controller needs."""

    domain: str
    min_working_tier: Tier = Tier.HTTP
    requires_proxy: bool = False
    required_proxy_type: str | None = None
    detected_waf: str | None = None
    # Which proxy country this domain will actually answer. Learned, not
    # configured: a geo-gate is invisible until you try a second country.
    working_country: str | None = None
    country_attempts: tuple[str, ...] = ()
    success_count: int = 0
    failure_count: int = 0
    block_count: int = 0
    avg_content_length: int | None = None
    stdev_content_length: int | None = None
    circuit_open_until: float | None = None  # monotonic-comparable epoch seconds
    # How long this domain actually takes when it succeeds. See
    # budget_for_domain: a domain whose only working rung costs 40s cannot be
    # served inside a budget set for domains that answer in one.
    avg_success_ms: int | None = None
    stdev_success_ms: int | None = None
    timed_success_count: int = 0
    # Consecutive content-successes that landed ABOVE the floor. See
    # apply_success: the floor has to be able to rise on evidence, not only
    # fall, or a domain pays for a doomed rung on every single request.
    climbs_above_floor: int = 0
    # Consecutive blocks AT the floor, since the last success. The mirror of
    # the above, and it was missing: `apply_success` raised the floor only on
    # repeated evidence while `apply_block` raised it on a single event.
    # Measured 9 Sep 2026 across 4,112 profiles — 226 of the 347 raised floors
    # had been raised by ONE block, reddit.com among them: 47 successes, one
    # block, pinned to `mobile`, the dearest rung there is.
    blocks_at_floor: int = 0
    # Consecutive breaker openings with no success in between. Each one
    # doubles the next open duration (see circuit_backoff_minutes). Cleared by
    # any content-success, so a site that heals is back on the short fuse.
    circuit_opens: int = 0
    # Whether the open breaker covers this ONE url or the whole host. The
    # caller is told a different thing in each case, and telling someone the
    # site is off-limits when one page is sends them chasing the wrong cause.
    circuit_is_url_only: bool = False

    def to_stats(self) -> DomainStats:
        return DomainStats(
            domain=self.domain,
            success_count=self.success_count,
            avg_content_length=self.avg_content_length,
            stdev_content_length=self.stdev_content_length,
        )

    @property
    def circuit_open(self) -> bool:
        return self.circuit_open_until is not None and time.time() < self.circuit_open_until


# Every Nth success at a raised floor, start one rung lower and see. Five keeps
# the cost of a wrong floor to one extra cheap request in five.
PROBE_EVERY = 5

# Consecutive successes a rung above the floor before the floor moves up. The
# floor could previously only ever fall, so a domain whose cheap rungs return a
# 200 full of nothing kept paying for them: ancestry.com sat at `stealth` with
# 199 successes, every one of them served by `stealth_hard` one rung higher, so
# every request bought a doomed stealth attempt first (measured 7 Sep 2026).
# Three in a row, not one — a single expensive success is exactly the wrong
# call to write into shared state, and PROBE_EVERY still walks it back down.
RAISE_FLOOR_AFTER = 3

# Successful, timed requests before a domain's own timing is trusted over the
# default. Three is enough to tell a slow domain from one slow request.
BUDGET_MIN_SAMPLES = 3
# How far above the mean to aim. Two deviations covers the ordinary spread
# without letting one pathological fetch set the budget for every later one.
BUDGET_DEVIATIONS = 2
# Nothing learned may exceed this, whatever the observations say. A domain that
# wants five minutes is a domain to fix, not to wait for.
BUDGET_CEILING_MS = 240_000

# A challenge at the deepest rungs is a COIN TOSS, not a verdict. Measured on
# g2.com, 7 Sep 2026, one fetch per fresh residential session:
#     stealth_hard, /categories/crm       4 of 6 returned the real page
#     stealth_hard, /products/*/reviews   1 of 5
# The same rung, the same code, the same second — the difference is which exit
# IP the session landed on. Climbing cannot help (there is nothing above), and
# giving up throws away a two-in-three chance on the pages that matter. Each
# fetch already mints a new proxy session, so simply asking again is a new IP.
CHALLENGE_RETRY_TIERS = frozenset({Tier.STEALTH_HARD, Tier.MOBILE})
# Three attempts turns 4-in-6 into ~96% and 1-in-5 into ~49%. Beyond that the
# marginal page costs more than it is worth.
CHALLENGE_RETRIES = 2
# A device-fingerprint check belongs here for the same reason DataDome does:
# it is decided per exit IP, so a fresh session is a real second chance.
# Membership is matched against the verdict's vendor AND its signal (see the
# gate below): a body signature sets both, and testing only the vendor made
# every "generic"-vendor entry here dead letter.
CHALLENGE_SIGNALS = frozenset(
    {"datadome", "generic_captcha", "challenge_title", "device_verification"}
)

# WAFs that decide PER EXIT IP, so that on a domain already known to run one, a
# bare refusal at a deep rung is the same coin toss a recognised challenge is.
# The retry above keyed only on the RESPONSE, and g2.com — measured live 18 Sep
# 2026, the first day stealth_hard could run there — answers with a plain 403
# and no challenge markup, so `status_403` never matched and the request gave
# up after one IP on the rung that clears g2's review pages one time in five.
# The domain's profile already said DataDome; the response just didn't repeat
# it. Deliberately only the measured vendor: an unknown domain's 403 may be a
# genuine refusal, and asking three times would triple the cost of hearing no.
PER_IP_WAFS = frozenset({"datadome"})
BARE_REFUSAL_SIGNALS = frozenset({"status_403"})

# Walls that no rung and no exit gets through, so climbing is spending the
# caller's credits to be told the same thing six times. Google's results page
# answers with this from every tier we have and from residential exits in the
# country asked for (measured across stealth, stealth_hard and mobile, and
# again through a US residential exit, 20 Sep 2026): one request burned five
# rungs, took minutes, and then filled the failure window so the breaker shut
# the whole of google.com — including the paths that read perfectly well.
#
# Deliberately a measured list, not a guess. A signal belongs here only once
# the whole ladder has been watched failing on it; anything less and we stop
# climbing on a wall a higher rung would have cleared, which is the more
# expensive mistake.
TERMINAL_SIGNALS = frozenset({"google_unusual_traffic"})

# The 5xx codes that mean "try again in a second", not "this URL is broken".
# Statuses that mean "not right now" rather than "no": one retry at the same
# tier after a pause. 202 belongs here — accepted-and-processing very often
# becomes a 200 on the second ask, and climbing a tier buys nothing.
TRANSIENT_STATUSES = frozenset({"status_202", "status_502", "status_503", "status_504"})
TRANSIENT_RETRY_PAUSE_MS = 1_000

# How far past its budget a tier attempt may run before it is cut off. Each
# fetcher is GIVEN its budget, but honouring it was left to the fetcher, and
# the work after the page loads — measuring, reading the DOM — had no bound at
# all: one page with a streaming video ran for many minutes against a
# 90-second request.
# The grace covers a fetcher closing its browser cleanly; beyond it the attempt
# is cancelled, because a request nobody is waiting for still costs bandwidth.
TIER_OVERRUN_GRACE_MS = 15_000


@dataclass
class Attempt:
    tier: Tier
    verdict: Verdict
    latency_ms: int
    status_code: int | None
    bytes_transferred: int
    # The fetch_log row this attempt wrote, so a later verdict can amend it.
    # A tier's transport outcome is decided here, but whether the BODY was
    # content is only known after extraction — and the log said "success" for
    # a fetch the caller was told was BLOCKED (measured, Sep 2026).
    log_id: int | None = None
    # What went wrong in transport, and through which proxy. Without these the
    # proxy accounting only ever saw `outcome.result`, which is None on EVERY
    # failed request — so a provider rejecting our password, or refusing to
    # carry a target, reached the health check as "no error" and was scored as
    # working. The proxy is the one the attempt USED, which for the stealth
    # tiers is an exit they chose themselves.
    error: str | None = None
    proxy_id: str | None = None
    # ...and what kind of exit it was, so the bandwidth ledger can price it.
    proxy_type: str | None = None
    # WHICH url this attempt was for. The fetch log recorded an empty hash on
    # every row, so the only history we had was "something on this domain
    # failed" — enough to open the breaker, never enough to tell a hostile
    # path from the rest of the site. See repositories.url_worked_recently.
    url: str | None = None


def _provider_refused(error: str | None) -> bool:
    """Did the proxy provider decline this target? False without a proxy layer."""
    try:
        from engine.core.proxy.providers import refuses_target
    except ImportError:
        return False
    return refuses_target(error)


@dataclass
class EscalationOutcome:
    result: FetchResult | None
    verdict: Verdict
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def tiers_attempted(self) -> list[str]:
        return [str(a.tier) for a in self.attempts]

    @property
    def succeeded(self) -> bool:
        return self.verdict.ok and self.result is not None

    @property
    def total_bytes(self) -> int:
        return sum(a.bytes_transferred for a in self.attempts)

    @property
    def proxied_bytes(self) -> int:
        """Only the attempts that went out through an exit: what a vendor bills.

        `total_bytes` counts the direct rungs too, and was reported as the
        request's proxy bytes whenever the serving rung was proxied — an
        archive fetched direct twice and then once through a stealth exit was
        reported as 19 MB of proxy traffic for 5 MB on the invoice.
        """
        return sum(a.bytes_transferred for a in self.attempts if a.proxy_id)


def _waf_min_tier() -> dict[str, Tier]:
    """Which tier clears which WAF — proprietary knowledge, optional here.

    Measured per vendor, so it lives in engine/knowledge with the block
    signatures rather than in the escalation machinery that reads it. Without
    it the ladder still climbs on evidence; it just pays for the cheaper rung
    first on the handful of vendors known to refuse it.
    """
    try:
        from engine.knowledge.waf_tiers import WAF_MIN_TIER
    except ImportError:
        return {}
    return WAF_MIN_TIER


def ladder_from(
    start: Tier, *, available: set[Tier] | None = None, max_tier: Tier | None = None
) -> list[Tier]:
    """The tier sequence from a starting point, restricted to what is built.

    If nothing at or above `start` is available — a domain profile raised to
    `browser` while only the HTTP tiers are deployed — fall back to the single
    best tier we do have rather than returning an empty ladder.

    An empty ladder is a self-inflicted outage: the controller reports BLOCKED
    without making a single request, so every page on that domain fails
    instantly and the failure looks exactly like the target blocking us.
    Trying the best available tier may not clear the site, but it fails for a
    real reason, on real evidence.
    """
    index = TIER_ORDER.index(start)
    tiers = list(TIER_ORDER[index:])
    # A ceiling is a spending decision, not a capability one: a bulk pass that
    # would rather skip a page than pay for a residential exit says so here.
    # Never returns empty — a ceiling below the start still tries the start,
    # because an empty ladder reports BLOCKED without making a request and that
    # is indistinguishable from the target actually blocking us.
    if max_tier is not None:
        capped = [t for t in tiers if TIER_ORDER.index(t) <= TIER_ORDER.index(max_tier)]
        # Empty when the ceiling sits below the cheapest rung this domain can
        # be served by. That used to fall back to `tiers[:1]` — the starting
        # tier, ABOVE the ceiling — so a caller who set maxTier as a spending
        # limit was billed past it without being told. The caller hears about
        # it instead; see `tier_ceiling_below_floor`.
        if not capped:
            # The ceiling forbids every rung from the start upward. Return
            # nothing, and do NOT fall through to the "drop to a lower rung"
            # rescue below — that rescue exists for a tier this deployment has
            # not built, and using it here would quietly serve the request
            # from a rung the caller's own ceiling was meant to prevent.
            return []
        tiers = capped
    if available is None:
        return tiers

    usable = [t for t in tiers if t in available]
    if usable:
        return usable

    # The starting rung is not built here. Drop to the best one that is —
    # still under the caller's ceiling, which applies to the rescue too.
    below = [t for t in TIER_ORDER[:index] if t in available]
    if max_tier is not None:
        below = [t for t in below if TIER_ORDER.index(t) <= TIER_ORDER.index(max_tier)]
    return [below[-1]] if below else []


def starting_tier(
    profile: DomainProfile,
    forced: Tier | None = None,
    *,
    floor: Tier | None = None,
) -> Tier:
    """Where to begin. Never below what the domain has historically needed —
    except, every PROBE_EVERY successes, one rung below it.

    A floor rises on a block and, until now, fell only in the weekly decay, so a
    domain that blocked twice at tier 0 (or was wrongly REPORTED blocked, which
    the detector has done) paid browser prices for up to a week: 94 of 737
    profiles sat at a raised floor on 6 Sep 2026. The probe is one cheap request
    that either succeeds — and apply_success lowers the floor — or fails and
    climbs back within the same request. apply_block ignores a failed probe,
    because the probe tier is below the floor.
    """
    if forced is not None:
        return forced
    start = profile.min_working_tier
    # Cadence counts REQUESTS, not successes. `and profile.success_count` made
    # zero falsy, so a domain that had only ever been blocked could never probe
    # back down — and that is exactly the population a wrong verdict strands:
    # 125 domains sat on a raised floor with no WAF vendor recorded at all
    # (7 Sep 2026), paying the 5-credit rung for a judgement about their
    # CONTENT. A probe is one cheap request that either succeeds and lowers the
    # floor, or fails and climbs straight back inside the same request.
    # `seen` must be non-zero: a floor raised a moment ago has nothing to
    # reconsider, and probing it immediately would undo the learning in the
    # same breath as acquiring it.
    seen = profile.success_count + profile.block_count
    if TIER_ORDER.index(start) > 0 and seen and seen % PROBE_EVERY == 0:
        start = next_tier_down(start)
    if profile.detected_waf:
        waf_floor = _waf_min_tier().get(profile.detected_waf.lower())
        if waf_floor and TIER_ORDER.index(waf_floor) > TIER_ORDER.index(start):
            start = waf_floor
    # The FLOOR the request itself imposes: the cheapest rung that can honour
    # what was asked for. This was a `force_browser` boolean, which could only
    # express one of the two floors we have. `mobile: true` needs the second:
    # tier 0 sends our honest bot identity and has no phone to be, so a caller
    # asking for the phone view got the desktop page and no sign that they
    # had — the same silent drop `waitFor` used to suffer at these rungs.
    if floor is not None and TIER_ORDER.index(start) < TIER_ORDER.index(floor):
        start = floor
    return start


def budget_for_tier(tier: Tier, remaining_ms: int) -> int:
    """Divide the total timeout so every rung still to come keeps its minimum.

    The old split handed each browser-class rung the WHOLE remainder, so the
    stealth rung consumed everything and stealth_hard — the one rung DataDome
    domains actually need — started with nothing, whatever the caller's
    timeout. A rung now takes what is left after reserving the minimum viable
    time of every rung above it, never less than its own minimum.
    """
    if tier in (Tier.HTTP, Tier.IMPERSONATE):
        return min(remaining_ms, settings.tier0_max_ms)
    later = TIER_ORDER[TIER_ORDER.index(tier) + 1 :]
    reserve = sum(MIN_TIER_TIME_MS[t] for t in later)
    return max(remaining_ms - reserve, MIN_TIER_TIME_MS[tier])


class EscalationController:
    """Runs a request up the ladder, stopping at the first genuine success."""

    def __init__(
        self,
        fetchers: dict[Tier, Fetcher],
        *,
        on_attempt: Callable[[str, Attempt], Awaitable[None]] | None = None,
    ) -> None:
        self._fetchers = fetchers
        self._on_attempt = on_attempt

    async def fetch(
        self,
        req: FetchRequest,
        profile: DomainProfile,
        *,
        forced_tier: Tier | None = None,
        escalate: bool = True,
        floor: Tier | None = None,
        max_tier: Tier | None = None,
    ) -> EscalationOutcome:
        if profile.circuit_open:
            remaining = max(0, int((profile.circuit_open_until or 0) - time.time()))
            return EscalationOutcome(
                result=None,
                verdict=Verdict(
                    ok=False,
                    reason=Reason.BLOCKED,
                    signal="url_backoff_open" if profile.circuit_is_url_only else "circuit_open",
                    confidence=1.0,
                    # The real remaining time. With backoff this can be a day,
                    # and a message quoting the 15-minute base would be a lie
                    # the caller would act on.
                    details={
                        "domain": profile.domain,
                        "retry_after_s": remaining,
                        "consecutive_opens": profile.circuit_opens,
                    },
                ),
            )

        start = starting_tier(profile, forced_tier, floor=floor)
        available = set(self._fetchers)
        ladder = ladder_from(start, available=available, max_tier=max_tier)
        if not escalate:
            ladder = ladder[:1]

        if not ladder:
            ceiling_too_low = max_tier is not None and TIER_ORDER.index(
                max_tier
            ) < TIER_ORDER.index(start)
            return EscalationOutcome(
                result=None,
                verdict=Verdict(
                    ok=False,
                    reason=Reason.BLOCKED,
                    signal=("tier_ceiling_below_floor" if ceiling_too_low else "no_tier_available"),
                    confidence=1.0,
                    details={
                        "requested_start": str(start),
                        **({"max_tier": str(max_tier)} if ceiling_too_low else {}),
                    },
                ),
            )

        attempts: list[Attempt] = []
        budget_ms = req.timeout_ms
        stats = profile.to_stats()
        last_verdict = Verdict(ok=False, reason=Reason.BLOCKED, signal="not_attempted")
        retried_tiers: set[Tier] = set()
        challenge_retries: dict[Tier, int] = {}

        tier_queue = list(ladder)
        # Set by a challenge retry: the next attempt must leave on a NEW exit.
        # Resending the same request resent the same session, so the same IP,
        # and the fresh coin toss the retry exists for never happened.
        fresh_exit = False
        while tier_queue:
            tier = tier_queue.pop(0)
            fetcher = self._fetchers.get(tier)
            if fetcher is None:
                continue

            minimum = MIN_TIER_TIME_MS[tier]
            # The minimum-time rule governs ESCALATION, not the first attempt.
            # A caller who asked for a short timeout still gets one try with
            # the budget they gave; refusing to fetch at all would be a
            # surprise. Beyond the first attempt, starting a tier that cannot
            # finish just burns the remainder and returns nothing.
            if attempts and budget_ms < minimum:
                # Out of time — but the rungs we DID run learned something, and
                # apply_block writes `verdict.vendor` into the profile. Replacing
                # the last real verdict with an anonymous one threw that away:
                # g2.com hit a genuine DataDome challenge at tier 1, ran out of
                # budget before the top rung, and was recorded with no vendor —
                # so it never got the floor that would have let it start there,
                # and timed out the same way on every later request.
                prior = last_verdict
                last_verdict = Verdict(
                    ok=False,
                    reason=Reason.BLOCKED,
                    # Not the proxy BANDWIDTH budget (proxy/budget.py raises
                    # that); this is the caller's deadline. Same word for both
                    # sent a reader through three usage tables for nothing.
                    signal="deadline_exceeded",
                    confidence=1.0,
                    vendor=prior.vendor if prior is not None else None,
                    details={
                        "remaining_ms": budget_ms,
                        "tier": str(tier),
                        "last_signal": prior.signal if prior is not None else None,
                    },
                )
                break

            tier_budget = min(budget_for_tier(tier, budget_ms), budget_ms)
            attempt_req = FetchRequest(**{**req.__dict__, "timeout_ms": tier_budget})
            if fresh_exit:
                # The deep rungs mint their own exit, with a new session, when
                # the request carries none — so drop the one that was refused.
                attempt_req = FetchRequest(
                    **{
                        **attempt_req.__dict__,
                        "proxy_url": None,
                        "proxy_id": None,
                        "proxy_type": None,
                        "exit_chosen_by_fetcher": True,
                    }
                )
                fresh_exit = False

            started = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    fetcher.fetch(attempt_req),
                    timeout=(tier_budget + TIER_OVERRUN_GRACE_MS) / 1000,
                )
            except TimeoutError:
                logger.warning(
                    "tier_overran_budget",
                    domain=profile.domain,
                    tier=str(tier),
                    budget_ms=tier_budget,
                )
                result = FetchResult(
                    url=attempt_req.url,
                    status_code=None,
                    headers={},
                    body=b"",
                    content_type=None,
                    tier=str(tier),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    # Unknown: the attempt was cancelled before it could report.
                    bytes_transferred=0,
                    proxy_id=attempt_req.proxy_id,
                    proxy_type=attempt_req.proxy_type,
                    error=f"timeout: tier overran its {tier_budget}ms budget",
                )
            spent_ms = int((time.monotonic() - started) * 1000)
            budget_ms -= spent_ms

            verdict = validate(result, stats)
            attempt = Attempt(
                tier=tier,
                verdict=verdict,
                latency_ms=spent_ms,
                status_code=result.status_code,
                bytes_transferred=result.bytes_transferred,
                error=result.error,
                proxy_id=result.proxy_id,
                proxy_type=result.proxy_type,
                url=attempt_req.url,
            )
            attempts.append(attempt)
            if self._on_attempt is not None:
                await self._on_attempt(profile.domain, attempt)

            if verdict.ok:
                return EscalationOutcome(result=result, verdict=verdict, attempts=attempts)

            last_verdict = verdict

            # A research checkbox request gets one session, not repeated clicks
            # across exits or tiers — once the step has RUN. A connection that
            # never reached the page clicked nothing, so it climbs as usual.
            # Keep the normal action-error diagnosis below.
            if (
                req.captcha_state.get("attempted") or (result.action_results or {}).get("captcha")
            ) and not result.action_error:
                return EscalationOutcome(result=result, verdict=verdict, attempts=attempts)

            # A proxy provider refusing THIS target — its tunnel answered 403 —
            # refuses it at every rung. Climbing buys a slower refusal, not a
            # page. Measured 11 Sep 2026: paypal.com walked the whole ladder
            # through one provider, four refusals and 75 seconds, before the service
            # could route it to the next provider. Stop at the first; the
            # caller's retry-past-refusal takes it from here.
            if _provider_refused(result.error):
                return EscalationOutcome(result=None, verdict=verdict, attempts=attempts)

            # A transient 5xx — 502, 503, 504 — is retried ONCE at the same tier
            # after a short pause. An origin that hiccupped answers the second
            # request; one that is down answers the same, and we stop. Nothing
            # above tier 0 fixes an origin error, so this never escalates.
            if (
                verdict.reason == Reason.TARGET_ERROR
                and verdict.signal in TRANSIENT_STATUSES
                and tier not in retried_tiers
                and budget_ms >= minimum + TRANSIENT_RETRY_PAUSE_MS
            ):
                retried_tiers.add(tier)
                await asyncio.sleep(TRANSIENT_RETRY_PAUSE_MS / 1000)
                budget_ms -= TRANSIENT_RETRY_PAUSE_MS
                tier_queue.insert(0, tier)
                continue

            # The caller's own step failed. No rung can fix a selector that
            # matches nothing, and retrying one billed a typo twice.
            if result.action_error:
                return EscalationOutcome(
                    result=result,
                    verdict=Verdict(
                        ok=False,
                        reason=Reason.BLOCKED,
                        signal="action_failed",
                        confidence=1.0,
                        details={
                            "error": result.action_error,
                            "fault": result.action_fault or "invalid",
                            "actions": result.action_results,
                        },
                    ),
                    attempts=attempts,
                )

            # A wall nothing clears is the answer, however early we hit it.
            # Before the challenge retry below, which would otherwise spend
            # three fresh exits on it, and before the climb.
            if verdict.signal in TERMINAL_SIGNALS:
                logger.info(
                    "terminal_signal",
                    domain=profile.domain,
                    tier=str(tier),
                    signal=verdict.signal,
                    saved_tiers=len(tier_queue),
                )
                return EscalationOutcome(result=result, verdict=verdict, attempts=attempts)

            # A challenge at a deep rung is worth asking again, on a new exit.
            # Not an escalation — there is nothing above these rungs — and not
            # a retry of the same request: the fetcher mints a fresh proxy
            # session per call, so attempt two lands on a different IP. Only at
            # the rungs where a pass is actually possible; retrying a challenge
            # at tier 0 buys nothing but latency.
            if (
                verdict.reason in (Reason.BLOCKED, Reason.SOFT_BLOCK)
                # BOTH, not vendor-or-signal: a matched body signature sets
                # vendor AND signal (validator builds it with vendor=sig.vendor,
                # signal=sig.id), so `vendor or signal` short-circuited on a
                # truthy vendor and only ever tested that. Every signature
                # declaring the default vendor "generic" was therefore
                # unretryable by id — generic_captcha included, listed here
                # since the day it was written and never once matching.
                and (
                    bool({verdict.vendor, verdict.signal} & CHALLENGE_SIGNALS)
                    # ...or a bare refusal from a domain whose WAF is on
                    # record as deciding per IP. See PER_IP_WAFS.
                    or (
                        verdict.signal in BARE_REFUSAL_SIGNALS
                        and (profile.detected_waf or "").lower() in PER_IP_WAFS
                    )
                )
                and tier in CHALLENGE_RETRY_TIERS
                and challenge_retries.get(tier, 0) < CHALLENGE_RETRIES
                and budget_ms >= minimum
            ):
                challenge_retries[tier] = challenge_retries.get(tier, 0) + 1
                fresh_exit = True
                logger.info(
                    "challenge_retry",
                    domain=profile.domain,
                    tier=str(tier),
                    attempt=challenge_retries[tier],
                    signal=verdict.signal,
                    remaining_ms=budget_ms,
                )
                tier_queue.insert(0, tier)
                continue

            # A genuine target error is the answer, not a reason to climb.
            if verdict.reason == Reason.TARGET_ERROR:
                return EscalationOutcome(result=result, verdict=verdict, attempts=attempts)
            if verdict.reason == Reason.ROBOTS_DENIED:
                return EscalationOutcome(result=None, verdict=verdict, attempts=attempts)

            # A network timeout on the first attempt is retried at the SAME
            # tier before escalating. Retrying a flaky connection at tier 3
            # costs a hundred times more and fixes nothing.
            if (
                verdict.reason == Reason.EMPTY
                and verdict.signal == "transport_error"
                and tier not in retried_tiers
                and budget_ms >= minimum
            ):
                retried_tiers.add(tier)
                tier_queue.insert(0, tier)
                continue

        return EscalationOutcome(result=None, verdict=last_verdict, attempts=attempts)


# --------------------------------------------------------------------------
# Profile updates (03-fetch-tiers.md section 7)
# --------------------------------------------------------------------------


def next_tier_up(tier: Tier) -> Tier:
    index = TIER_ORDER.index(tier)
    return TIER_ORDER[min(index + 1, len(TIER_ORDER) - 1)]


def next_tier_down(tier: Tier) -> Tier:
    index = TIER_ORDER.index(tier)
    return TIER_ORDER[max(index - 1, 0)]


def apply_success(
    profile: DomainProfile, tier: Tier, content_length: int, *, chosen: bool = True
) -> DomainProfile:
    """On success: count it, and move the floor toward the rung that worked.

    `tier` is the rung that produced ACCEPTED CONTENT, not merely a 200 — the
    caller passes the serving tier after the content verdict. That distinction
    is the whole point here: ancestry's cheap rungs answer 200 with a nav shell,
    so the fetch log calls them successes while the page is worthless. A floor
    learned from transport outcomes would sit at `http` and be wrong every time.
    """
    profile.success_count += 1
    served = TIER_ORDER.index(tier)
    floor = TIER_ORDER.index(profile.min_working_tier)
    if served < floor:
        # Cheaper than we thought: take it immediately. One cheap success is
        # proof, and being wrong costs one wasted cheap request.
        profile.min_working_tier = tier
        profile.climbs_above_floor = 0
    elif served > floor and not chosen:
        # The CALLER forced this rung. It worked because it was asked for, not
        # because the domain needs it, so it is no evidence for raising the
        # floor. Measured 22 Sep 2026: a day of forced-rung benchmarks put
        # example.com's floor at `browser` and every later request paid five
        # credits and eighteen seconds for a page the plain rung serves. A
        # forced CHEAPER rung that works is real evidence, and is still taken
        # by the branch above.
        profile.climbs_above_floor = 0
    elif served > floor:
        # The floor is too low and every request is paying for it. Raise it one
        # rung on repeated evidence — never straight to `tier`, so a one-off
        # hard climb cannot pin a whole domain to the expensive end.
        profile.climbs_above_floor += 1
        if profile.climbs_above_floor >= RAISE_FLOOR_AFTER:
            # Move to ONE RUNG BELOW the tier that keeps serving, not one rung
            # up from the floor. ancestry.co.uk walks http -> impersonate ->
            # browser -> stealth -> stealth_hard -> mobile and only `mobile`
            # returns a page: at one rung per run of successes that is fifteen
            # requests to converge, each paying ~60s for the rungs underneath.
            # A rung of headroom is kept deliberately so a site that gets
            # easier is still caught by the cheaper attempt, and PROBE_EVERY
            # goes lower still.
            target = TIER_ORDER.index(next_tier_down(tier))
            profile.min_working_tier = TIER_ORDER[max(floor + 1, min(target, served))]
            profile.climbs_above_floor = 0
    else:
        profile.climbs_above_floor = 0

    # Any content-success ends a run of blocks: the streak is "this rung has
    # stopped working", and it plainly has not.
    profile.blocks_at_floor = 0
    # ...and puts the breaker back on its shortest fuse. The backoff exists
    # for domains that never work; one that just did is not one of them.
    profile.circuit_opens = 0

    # Running mean and standard deviation via Welford, so the baseline can be
    # maintained without holding every observation.
    n = profile.success_count
    if profile.avg_content_length is None:
        profile.avg_content_length = content_length
        profile.stdev_content_length = 0
    else:
        mean = profile.avg_content_length
        new_mean = mean + (content_length - mean) / n
        old_var = (profile.stdev_content_length or 0) ** 2
        new_var = ((n - 1) * old_var + (content_length - mean) * (content_length - new_mean)) / n
        profile.avg_content_length = int(new_mean)
        profile.stdev_content_length = int(new_var**0.5)
    return profile


def record_success_time(profile: DomainProfile, elapsed_ms: int) -> DomainProfile:
    """Fold one successful request's wall time into the domain's own timing.

    Welford, matching the content-length baseline: the mean and deviation are
    maintained without holding every observation. Only SUCCESSES count — a
    blocked request tells us how long we were willing to wait, not how long the
    domain needs.
    """
    profile.timed_success_count += 1
    n = profile.timed_success_count
    if profile.avg_success_ms is None:
        profile.avg_success_ms = elapsed_ms
        profile.stdev_success_ms = 0
        return profile
    mean = profile.avg_success_ms
    new_mean = mean + (elapsed_ms - mean) / n
    old_var = (profile.stdev_success_ms or 0) ** 2
    new_var = ((n - 1) * old_var + (elapsed_ms - mean) * (elapsed_ms - new_mean)) / n
    profile.avg_success_ms = int(new_mean)
    profile.stdev_success_ms = int(new_var**0.5)
    return profile


def budget_for_domain(profile: DomainProfile, requested_ms: int, *, caller_set: bool) -> int:
    """The deadline this request should actually get.

    A caller who names a timeout gets exactly it — that is a promise, not a
    hint. Otherwise a domain that has repeatedly proved it needs longer than the
    default gets what it needs, and every other domain is untouched. Raising the
    global default instead would spend that time on the sixteen hundred domains
    that answer in under a second.

    Never shortens: a domain that turns out to be fast keeps the default, since
    finishing early costs nothing and a too-tight budget costs the whole request.
    """
    if caller_set:
        return requested_ms
    if profile.timed_success_count < BUDGET_MIN_SAMPLES or profile.avg_success_ms is None:
        return requested_ms
    needed = profile.avg_success_ms + BUDGET_DEVIATIONS * (profile.stdev_success_ms or 0)
    return max(requested_ms, min(int(needed), BUDGET_CEILING_MS))


def apply_block(profile: DomainProfile, tier: Tier, vendor: str | None = None) -> DomainProfile:
    """On a block: count it, and raise the floor only once the rung has
    actually stopped working.

    It used to raise on the FIRST block, which is a claim about a domain made
    from a single event. Blocks are frequently probabilistic — a colleague
    measured pranx.com refusing at every rung one day and answering first try
    the next — and the cost of believing one is not symmetric:

      floor too LOW   one cheap wasted attempt, and the request still climbs
                      and succeeds inside itself
      floor too HIGH  every later request to that domain pays the dear rung,
                      for thirty days, until the decay

    So the evidence bar for RAISING is the same `RAISE_FLOOR_AFTER` the
    success path has always used, and a success resets it. At a 20% block rate
    a floor now moves on roughly one attempt in a hundred instead of one in
    five.
    """
    profile.block_count += 1
    if vendor:
        profile.detected_waf = vendor

    if tier != profile.min_working_tier:
        # A block above the floor says nothing about whether the floor works.
        return profile

    profile.blocks_at_floor += 1
    if profile.blocks_at_floor >= RAISE_FLOOR_AFTER:
        profile.min_working_tier = next_tier_up(tier)
        profile.blocks_at_floor = 0
    return profile


def circuit_backoff_minutes(opens: int) -> int:
    """How long the breaker stays open, given how many times it has opened
    in a row without a success.

    Doubles each time from `circuit_open_minutes`, capped at
    `circuit_open_max_minutes`. A flat 15 minutes was the whole of the old
    policy, and for a domain that has NEVER worked it meant sixteen full-price
    retries an hour, for ever, learning the same thing each time.

        opens  1     2     3     4      5      6      7      8+
        mins   15    30    60    120    240    480    960    1440 (cap)
    """
    base = settings.circuit_open_minutes
    cap = settings.circuit_open_max_minutes
    if opens <= 1:
        return min(base, cap)
    # int ** int types as Any in typeshed — a negative exponent would return
    # float, which mypy can't rule out from the type alone. The guard above
    # already guarantees opens > 1 here, so the exponent is always >= 1.
    return int(min(base * (2 ** min(opens - 1, 30)), cap))


def should_open_circuit(recent_outcomes: list[bool]) -> bool:
    """Failure rate over 50% across the last 20 attempts opens the breaker.

    The last twenty ATTEMPTS — not the attempts in the last five minutes. The
    window used to be wall-clock, and a domain that failed steadily at three
    or four requests a minute never filled it: wisdomlib.org peaked at 19
    attempts in a five-minute window against a threshold of 20, and the
    breaker did not open once in 542 consecutive failures. A breaker blind to
    slow, steady failure is blind to the case it exists for.
    """
    window = recent_outcomes[-settings.circuit_window :]
    if len(window) < settings.circuit_window:
        return False
    failures = sum(1 for ok in window if not ok)
    return (failures / len(window)) > settings.circuit_failure_rate


def decay_profile(profile: DomainProfile, days_since_last_block: float) -> DomainProfile:
    """Weekly decay: a domain quiet for 30 days drops a tier and re-proves.

    Without this, a domain that removed its WAF costs browser-tier money for
    ever.
    """
    if days_since_last_block >= 30 and profile.min_working_tier != Tier.HTTP:
        profile.min_working_tier = next_tier_down(profile.min_working_tier)
    return profile
