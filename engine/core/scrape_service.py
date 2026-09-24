"""The scrape pipeline — everything a single URL goes through.

Order matters and is load-bearing:

    SSRF guard -> robots -> cache -> politeness -> escalate/fetch
    -> validate (pre-extraction) -> extract -> validate (post-extraction)
    -> profile update -> store

The two validation passes are deliberate (05-block-detection.md s1): the cheap
one avoids paying for extraction on an obvious challenge page, and the second
catches soft blocks that are only visible once you have the extracted text.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import random
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from engine.core import change_tracking, metrics, url_backoff
from engine.core import robots as robots_mod
from engine.core.detect.validator import (
    DomainStats,
    ExtractionSummary,
    Reason,
    Verdict,
    extract_title,
    validate,
)
from engine.core.errors import (
    ENGINE_SIDE_SIGNALS,
    Blocked,
    EngineError,
    EngineRefused,
    ProxyUnavailable,
    RobotsDenied,
    TargetError,
)
from engine.core.extract.media import collect_media
from engine.core.extract.router import ExtractionResult, ExtractOptions, extract
from engine.core.extract.summary import summarise
from engine.core.fetch import consent, site_rules
from engine.core.fetch.base import Fetcher, FetchRequest, FetchResult
from engine.core.fetch.escalation import (
    Attempt,
    DomainProfile,
    EscalationController,
    EscalationOutcome,
    apply_block,
    apply_success,
    budget_for_domain,
    circuit_backoff_minutes,
    record_success_time,
    should_open_circuit,
)
from engine.core.models import (
    TIER_ORDER,
    ActionResults,
    Cost,
    ExtractionPath,
    MediaAsset,
    NetworkLog,
    NetworkRequest,
    PageMetadata,
    PageType,
    ProxyMode,
    ScrapeData,
    ScrapeOptions,
    Tier,
    TrackerHit,
)
from engine.core.politeness import HostBudget, PolitenessGate, budget_for_plan
from engine.core.ssrf import resolve_and_validate
from engine.core.urls import (
    host_of,
    normalize_url,
    normalized_hash,
    registrable_domain,
    url_hash,
    variant_hash,
)
from engine.settings import settings
from engine.storage import repositories as repo

logger = structlog.get_logger(__name__)

# How long to wait for a pacing slot before giving up on a synchronous
# request. Async crawl work requeues instead of waiting.
#
# Ten seconds was set when one host meant one request a second and two at a
# time. With the paid plans now issuing ten a second, a burst of twenty against
# one host queues legitimately — and at ten seconds nearly half of them were
# being dropped as failures. Measured: 11 of 20 survived at 10s.
MAX_POLITENESS_WAIT_MS = 30_000

# How many DISTINCT urls on one domain must return byte-identical content
# before that content is judged to be the site's chrome rather than a page.
# See ScrapeService._shared_body_verdict for why it is three and not two.
SHARED_BODY_MIN_URLS = 3

# THIN judges READABLE TEXT — too little of it, or none at all. A caller who
# asked only for a picture never cared: a dashboard that is genuinely mostly
# charts is nav_shell by word count and a perfectly good screenshot. Confirmed
# live against dashboard.tremor.so: 180 words, THIN every time, six real tier
# escalations (two of them browser-class) spent chasing text nobody asked
# for, and the screenshot itself was never even attempted — it is fetched
# AFTER this check succeeds. Deliberately narrow to Reason.THIN alone:
# SOFT_BLOCK/BLOCKED mean the page itself is suspect (a decoy, a challenge),
# and a screenshot of that is misleading content, not a page that just
# happens to be light on prose.
TEXT_DEPENDENT_FORMATS = frozenset({"markdown", "html", "links", "summary", "json"})


@dataclass
class ScrapeOutcome:
    data: ScrapeData
    page_id: str | None = None
    from_cache: bool = False
    attempts: list[Attempt] = field(default_factory=list)


class ScrapeService:
    def __init__(
        self,
        fetchers: dict[Tier, Fetcher],
        *,
        politeness: PolitenessGate | None = None,
        persist: bool = True,
    ) -> None:
        self._fetchers = fetchers
        self._politeness = politeness or PolitenessGate()
        self._persist = persist
        self._controller = EscalationController(fetchers, on_attempt=self._log_attempt)

    async def _log_attempt(self, domain: str, attempt: Attempt) -> None:
        if not self._persist:
            return
        outcome = "success" if attempt.verdict.ok else _outcome_for(attempt.verdict)
        metrics.record_attempt(str(attempt.tier), outcome, attempt.latency_ms / 1000)
        if outcome == "blocked":
            metrics.record_block(str(attempt.tier), attempt.verdict.signal)

        try:
            attempt.log_id = await repo.log_fetch_attempt(
                domain=domain,
                url_hash=url_hash(attempt.url) if attempt.url else b"",
                tier=str(attempt.tier),
                outcome=outcome,
                status_code=attempt.status_code,
                latency_ms=attempt.latency_ms,
                bytes_transferred=attempt.bytes_transferred,
                # Which exit carried it. `log_fetch_attempt` has taken this
                # since it was written and nothing ever passed it, so every
                # row said NULL: 3,300 attempts over 30 days, 100% of them
                # reading as direct, including the ones we know were proxied.
                # Same fault as the empty url_hash, and it hid the one number
                # that says how exposed the direct path actually is.
                proxy_id=attempt.proxy_id,
                block_signal=attempt.verdict.signal,
            )
        except Exception as exc:  # noqa: BLE001 - logging must never fail a fetch
            _log_swallowed("fetch_log write failed", exc)

    async def _amend_attempt_log(
        self, attempts: list[Attempt], result: FetchResult | None, verdict: Verdict
    ) -> None:
        """Tell the log what the validator decided about the body it recorded.

        The transport succeeded — 200, bytes, a real latency — and that is what
        the row says. Only extraction reveals the body was a challenge shell or
        a decoy, and by then the row is written. Amending the attempt that
        produced the returned result joins the two halves, so "why did this
        domain fail" has one answer instead of two contradictory ones.
        """
        if not self._persist or result is None or verdict.ok:
            return
        target = next(
            (a for a in reversed(attempts) if str(a.tier) == str(result.tier) and a.log_id),
            None,
        )
        # The generator above already filters on a.log_id being truthy, but a
        # truthy check inside a generator expression is not a type guard mypy
        # can carry to `target` afterwards — this makes the same invariant
        # explicit and provable rather than asserting past it.
        if target is None or target.log_id is None:
            return
        try:
            await repo.amend_fetch_outcome(target.log_id, _outcome_for(verdict), verdict.signal)
        except Exception as exc:  # noqa: BLE001 - logging must never fail a fetch
            _log_swallowed("fetch_log amend failed", exc)

    async def scrape(
        self,
        url: str,
        options: ScrapeOptions,
        *,
        job_id: str | None = None,
        worker: str = "api",
        plan_concurrency: int | None = None,
        owner_ref: str | None = None,
    ) -> ScrapeOutcome:
        # 1. SSRF guard. Resolves DNS and pins the addresses; checking the
        #    hostname alone is defeated by rebinding.
        request_started = time.monotonic()
        target = await resolve_and_validate(url)
        domain = registrable_domain(url)
        host = host_of(url)
        norm_hash = normalized_hash(url)
        cache_key, shareable = self._cache_variant(url, options)

        profile = await repo.load_domain_profile(domain) if self._persist else DomainProfile(domain)

        # A domain that has repeatedly proved it needs longer than the default
        # gets what it needs. etsy.com's floor is already right — DataDome
        # starts it at stealth_hard — but one deep attempt costs 15-50s and a
        # challenge there is a coin toss, so inside 90s only two or three tries
        # fit and a domain that would answer on the fourth returns BLOCKED.
        # Raising the global default would spend that on the 1,600 domains that
        # answer in under a second. A caller who NAMED a timeout keeps it.
        caller_set_timeout = "timeout" in getattr(options, "model_fields_set", set())
        granted_ms = budget_for_domain(profile, options.timeout, caller_set=caller_set_timeout)
        if granted_ms != options.timeout:
            logger.info(
                "learned_budget_applied",
                domain=domain,
                default_ms=options.timeout,
                granted_ms=granted_ms,
                samples=profile.timed_success_count,
            )
            options = options.model_copy(update={"timeout": granted_ms})

        # 2. robots.txt, before anything is fetched from the target.
        if options.respectRobots:
            rules = await self._robots_for(host, url)
            if not rules.permits(robots_mod.path_of(url)):
                raise RobotsDenied(url)
            if rules.crawl_delay_ms and self._persist:
                await repo.raise_politeness_delay(domain, rules.crawl_delay_ms)

        # 3. Cache. The cache IS the pages table.
        if self._persist and options.maxAge > 0 and not options.captchaEvidence:
            cached = await repo.cache_lookup(cache_key, options.maxAge)
            if cached is not None and _row_is_thin(cached):
                # Written before the THIN check existed, or by a rule since
                # tightened: 1,167 cached "successes" were navigation shells
                # under 200 words, served as hits and bypassing every verdict
                # (7 Sep 2026). A row is judged by the rules in force when it is
                # READ, not the ones in force when it was written.
                logger.info("cache_row_stale", url=url, word_count=cached["word_count"])
                cached = None
            if cached is not None and not _row_satisfies(cached, options):
                # The row is for this URL and this extraction, but it was stored
                # by a caller who asked for fewer FORMATS, so the column this
                # caller wants is NULL. Serving it would hand back an empty
                # `html` with `cached: true` — the silent under-delivery the
                # variant hash exists to prevent, one level further down.
                # Re-fetch and store the richer row; the next caller of either
                # shape is then satisfied from it.
                logger.info("cache_row_too_thin", url=url, formats=options.format_names)
                cached = None
            metrics.record_cache(cached is not None)
            if cached is not None:
                # A cache hit inside a job must still belong to the job. The
                # page rows are the crawl's results (`list_job_pages` filters on
                # job_id), so returning here without a row for THIS job makes
                # the page vanish from the crawl — the seed URL of the audited
                # audit had been scraped directly minutes earlier and was
                # absent from its own crawl's output (measured).
                if job_id:
                    await self._link_cached_page(cached, job_id, url)
                data = _data_from_page_row(cached, url, options, owner_ref=owner_ref)
                return ScrapeOutcome(
                    data=await self._deliver(
                        data,
                        options,
                        markdown=cached["markdown"],
                        html=_row_html(cached),
                    ),
                    page_id=cached["id"],
                    from_cache=True,
                )

        # 4. Politeness, enforced across all workers. The caller's plan sets the
        #    pace; the DOMAIN may still slow it down and never speed it up.
        budget = budget_for_plan(plan_concurrency)
        domain_delay, domain_concurrency = (
            await repo.get_politeness(domain) if self._persist else (None, None)
        )
        acquired = await self._acquire_politeness(domain, domain_delay, domain_concurrency, budget)
        if not acquired:
            raise Blocked(
                "Too many concurrent requests to this host: waited for a pacing "
                "slot and gave up. The target did not refuse us — slow down, or "
                "raise the plan's concurrency.",
                final_signal="politeness_timeout",
            )

        try:
            # 5. Escalate through the tier ladder.
            endpoint = await self._select_proxy(domain, options, profile)

            # Per-site defaults (site_rules.yaml): the consent/preference cookies
            # a first visit would set. Without them an EU exit is shown Google's
            # consent interstitial at every tier and calls it a block. A caller's
            # own Cookie header always wins over ours.
            rule = await site_rules.effective(url, persist=self._persist)

            fetch_req = FetchRequest(
                url=url,
                target=target,
                timeout_ms=options.timeout,
                headers=_with_site_cookie(dict(options.headers), rule),
                mobile=options.mobile,
                location=options.location,
                block_assets=options.blockAssets and not options.wants_screenshot,
                wait_for_ms=options.waitFor,
                capture_network=options.wants_network,
                actions=list(options.actions),
                captcha_handling=options.captchaHandling,
                captcha_evidence=options.captchaEvidence,
                proxy_url=endpoint.connection_url() if endpoint else None,
                proxy_id=endpoint.scored_as if endpoint else None,
                proxy_type=str(endpoint.type) if endpoint else None,
                cookies=rule.browser_cookies(),
            )
            # A breaker open on the DOMAIN must not bury a url that works.
            # One hostile path — a results page, a login wall — fills the
            # failure window on its own and everything else on the host is
            # refused behind it, for fifteen minutes and then for hours. A url
            # with a success of its own in the window gets a probe instead.
            # Bounded by that evidence: an unknown path on a failing domain is
            # still refused, which is the whole point of the breaker.
            attempt_profile = profile
            if self._persist:
                digest = url_hash(url)
                try:
                    # This url's own backoff first: a page that keeps failing
                    # is refused on its own account, through the same path and
                    # the same message the domain breaker uses.
                    until = await url_backoff.open_until(domain, digest)
                    if until is not None:
                        attempt_profile = replace(
                            profile, circuit_open_until=until, circuit_is_url_only=True
                        )
                    elif profile.circuit_open and await repo.url_worked_recently(domain, digest):
                        attempt_profile = replace(profile, circuit_open_until=None)
                        logger.info("circuit_half_open_probe", domain=domain, url=url)
                except Exception as exc:  # noqa: BLE001 - never fail a fetch over history
                    _log_swallowed("circuit probe lookup failed", exc)

            forced = None if options.tier == "auto" else Tier(options.tier)
            # A feed is XML, and no browser rung can return it: Chrome wraps the
            # document in its own viewer, so every rung above impersonate fails
            # while costing more. Capped unless the caller chose a ceiling.
            feed = _is_feed_url(url)
            feed_cap = Tier.IMPERSONATE if feed and options.maxTier is None else options.maxTier
            outcome = await self._controller.fetch(
                fetch_req,
                attempt_profile,
                forced_tier=forced,
                escalate=options.escalate,
                floor=options.tier_floor,
                max_tier=feed_cap,
            )
        finally:
            await self._politeness.release(domain)

        if endpoint is not None and self._persist:
            await self._record_proxy_outcome(
                endpoint.scored_as, str(endpoint.type), domain, outcome, job_id
            )
        elif self._persist:
            # The stealth tiers choose their own exit, so no endpoint was
            # recorded above — but each attempt still says which provider
            # carried it, and provider health needs to hear about it.
            await self._note_providers(outcome, domain)

        # 4b. A FEED that failed direct gets one pass through a residential exit
        #     at the HTTP tiers. Under `auto` a proxy is used only once a
        #     domain's profile says it needs one, and that is learned only when
        #     a stealth rung succeeds — which a feed never can. So reddit.com's
        #     .rss URLs climbed to stealth and failed on every request, never
        #     learning the one fact that would have fixed them. Measured 17 Sep
        #     2026: auto walked 4 rungs for 5 credits and 0 entries; residential
        #     at tier 0 took 1 rung, 2 credits, 32 entries.
        if (
            feed
            and not options.has_captcha_checkbox
            and not fetch_req.captcha_state.get("attempted")
            and self._persist
            and not outcome.succeeded
            and endpoint is None
            and options.proxy == ProxyMode.AUTO
        ):
            try:
                feed_endpoint = await self._select_proxy(
                    domain, options.model_copy(update={"proxy": ProxyMode.RESIDENTIAL}), profile
                )
            except ProxyUnavailable:
                feed_endpoint = None  # no exit to be had: the direct answer stands
            if feed_endpoint is not None:
                logger.info("feed_retry_via_proxy", domain=domain, proxy=feed_endpoint.describe())
                retried = await self._controller.fetch(
                    replace(
                        fetch_req,
                        proxy_url=feed_endpoint.connection_url(),
                        proxy_id=feed_endpoint.scored_as,
                        proxy_type=str(feed_endpoint.type),
                    ),
                    profile,
                    forced_tier=forced,
                    escalate=options.escalate,
                    floor=options.tier_floor,
                    max_tier=feed_cap,
                )
                await self._record_proxy_outcome(
                    feed_endpoint.scored_as, str(feed_endpoint.type), domain, retried, job_id
                )
                outcome = EscalationOutcome(
                    result=retried.result,
                    verdict=retried.verdict,
                    attempts=[*outcome.attempts, *retried.attempts],
                )
                if retried.succeeded:
                    # Learned now, so the next request on this domain routes a
                    # proxy from the start instead of failing direct first.
                    endpoint = feed_endpoint
                    profile.requires_proxy = True
                    profile.required_proxy_type = str(feed_endpoint.type)

        # 5a. A provider that refused THIS domain is not a reason to fail the
        #     request. The refusal is already recorded, so a fresh selection
        #     skips that provider; run the ladder once more on whatever comes
        #     next. Once, not in a loop: if the next provider refuses too, the
        #     one after that is tried on the NEXT request.
        if (
            self._persist
            and not options.has_captcha_checkbox
            and not fetch_req.captcha_state.get("attempted")
            and not outcome.succeeded
            and _hit_a_refusal(outcome)
        ):
            rescued = await self._retry_past_refusal(domain, options, profile, fetch_req, forced)
            if rescued is not None:
                outcome = rescued

        # 5b. A block might be a geo-gate rather than a bot defence. Measured:
        # etsy.com answered 0/10 from a GB residential IP and 10/10 from a US
        # one, at tier 1. Escalating that to a browser costs ~40x and still
        # fails, because the objection was never the fingerprint.
        #
        # Only on a block, only when the country is not already known, and only
        # over a short bounded list — a domain that answers from nowhere must
        # not be retried around the world on every request.
        if (
            self._persist
            and outcome.result is None
            and not outcome.verdict.ok
            and not options.has_captcha_checkbox
            and not fetch_req.captcha_state.get("attempted")
            and _is_geo_retryable(outcome.verdict)
        ):
            rescued = await self._retry_other_countries(
                url, target, options, profile, domain, _geo_probe_tier(self._fetchers)
            )
            if rescued is not None:
                outcome = rescued

        # 5c. A consent wall is a door, not a block. If the ladder ended on the
        #     consent host (the shipped cookie has rotated, or the site is new to
        #     us), click "Accept all" over HTTP, keep the cookie it sets, and
        #     retry once with it. Only for hosts that have a site rule — a wall
        #     we have never seen is a signal to look, not something to click.
        if (
            rule.cookies
            and not options.has_captcha_checkbox
            and not fetch_req.captcha_state.get("attempted")
            and (outcome.result is None or not outcome.verdict.ok)
            and _looks_like_consent(outcome)
        ):
            healed = await self._heal_consent(url, fetch_req, profile, options, rule, domain)
            if healed is not None:
                outcome, rule, fetch_req = healed

        if outcome.result is None or not outcome.verdict.ok:
            await self._record_failure(profile, outcome.verdict, outcome.attempts, domain)
            raise _error_with_captcha(outcome.verdict, outcome.tiers_attempted, outcome.result)

        result = outcome.result

        # 6 + 7. Extract, then validate again with the text in hand. This is
        #    where soft blocks and decoy content are caught. A local function
        #    because it can run twice: once on the rung that answered, and once
        #    more if that answer turns out to be a JS shell and a higher rung
        #    is available.
        def _pass_blocking(res: FetchResult) -> tuple[Any, ExtractionSummary, Verdict]:
            # A document, not a web page. We have a parser, we bill a
            # `pdf_page` rate, the docs promise PDFs come back as text and
            # `parsers` claims to control which run — and NOTHING connected
            # them to a scrape. Scraping a link to a PDF returned
            # EXTRACTION_FAILED, so every PDF in a crawl failed, on an engine
            # that could read it perfectly well through /v1/parse.
            parsed = _parse_document(res, options)
            if parsed is not None:
                return parsed

            verbatim = _verbatim_body(res, profile.to_stats())
            if verbatim is not None:
                return verbatim

            ext = extract(
                res.text(),
                res.url,
                ExtractOptions(
                    only_main_content=options.onlyMainContent,
                    include_tags=list(options.includeTags),
                    exclude_tags=list(options.excludeTags),
                    remove_base64_images=options.removeBase64Images,
                    baseline_mean=profile.avg_content_length,
                    baseline_stdev=profile.stdev_content_length,
                ),
            )
            summ = ExtractionSummary(
                markdown=ext.markdown,
                page_type=str(ext.page_type),
                external_link_count=_external_links(ext.links, res.url),
                has_images=bool(ext.classification and "![" in ext.markdown),
                has_author=bool(ext.author),
                word_count=ext.word_count,
                char_count=ext.char_count,
                confidence=ext.confidence,
                link_count=len(ext.links),
                title=ext.title or extract_title(res.text(limit=20_000)),
                extraction_path=str(ext.extraction_path),
            )
            return ext, summ, validate(res, profile.to_stats(), summ)

        async def _pass(res: FetchResult) -> tuple[Any, ExtractionSummary, Verdict]:
            """Extraction off the event loop.

            `extract()` is CPU-bound — trafilatura and selectolax walk the whole
            document — and this coroutine runs on the API's event loop. Run
            inline, a single 750 KB page blocks every other request AND the
            accept loop, so new connections are refused while /health still
            answers 200 whenever it wins a slice. That is exactly the failure
            the pilot hit on 7 Sep 2026: one uvicorn process pinned at 92% CPU,
            188 of 201 names lost to ECONNREFUSED, recovering on its own once
            the queue drained. /v1/extract and /v1/parse already went through a
            thread; this path, the busiest one, did not.
            """
            ext, summ, verdict = await asyncio.to_thread(_pass_blocking, res)
            if verdict.ok:
                verdict = await self._shared_body_verdict(domain, res.url, ext.markdown, verdict)
            return ext, summ, verdict

        extraction, summary, post_verdict = await _pass(result)

        # 7b. A 200 that turned out to be a JS shell, a consent wall or a decoy
        #     is a block the cheap rung could not see. The spec's escalation
        #     triggers include exactly this — "soft-block: 200 with content far
        #     below baseline" — but until now the ladder had already returned by
        #     the time we knew, so the engine stopped honestly and never climbed.
        #     Measured on a real target (5 Sep 2026): Meta's docs came back as a
        #     shell on `auto` and only rendered when the caller forced the
        #     browser. Climb once, to the next rung that is actually wired.
        # 7a. The wall can also show up here: a consent page that rendered as a
        #     200 and only revealed itself by title. Heal before climbing —
        #     a browser opens the same door and finds the same wall.
        if (
            not post_verdict.ok
            and rule.cookies
            and post_verdict.signal in ("challenge_title", "consent_wall")
        ):
            healed = await self._heal_consent(url, fetch_req, profile, options, rule, domain)
            if healed is not None and healed[0].result is not None and healed[0].verdict.ok:
                outcome, rule, fetch_req = healed
                # The check above narrows healed[0].result, not outcome.result —
                # a different expression for the same object once unpacked, which
                # mypy does not carry the narrowing across. Provable, not assumed:
                # nothing runs between the check and here that could change it.
                assert outcome.result is not None
                result = outcome.result
                extraction, summary, post_verdict = await _pass(result)

        # 7b. Climb while the page still reads as a shell. This was a single
        #     `if` — one rung, once. A nav shell at http climbed to browser; the
        #     browser returned "near_empty" from THIS pass (a verdict only the
        #     extraction can see); nothing climbed again, and a domain that
        #     needed stealth_hard failed two rungs short on any timeout
        #     (ancestry, 7 Sep 2026). Each climb now also inherits what is LEFT
        #     of the caller's deadline rather than a fresh full timeout.
        from engine.core.extract.heuristic import is_richer_extraction
        from engine.core.fetch.base import MIN_TIER_TIME_MS

        # The richest extraction any rung produced, kept apart from the loop's
        # own working state. A climb is chasing a block or a content-quality
        # complaint, and a JS-executing rung can legitimately return LESS than
        # a cheaper one did — a site's own client-side paywall script can
        # strip, in a real browser, prose the raw HTML already carried, and
        # the rung it strips it TO can still "validate" (real nav/search
        # furniture around almost no words skips the near-empty gate
        # entirely). Nothing before this compared rungs to each other; each
        # climb just trusted whichever one it landed on last.
        best_result, best_extraction, best_summary, best_verdict = (
            result,
            extraction,
            summary,
            post_verdict,
        )
        # Whether any climbed rung validated outright, however little it
        # carried. If one did, the richer candidate we keep instead cannot be
        # a quality regression on it — the system was already willing to
        # accept something with less.
        a_thinner_rung_validated = False

        climbs = 0
        while (
            not post_verdict.ok
            and post_verdict.reason in (Reason.SOFT_BLOCK, Reason.BLOCKED, Reason.THIN)
            # THIN alone is purely a TEXT-quality complaint — climbing tiers to
            # find better prose is real cost (browser-class rungs, proxy bytes)
            # spent on a page nobody asked to read. SOFT_BLOCK/BLOCKED still
            # climb regardless of formats: a higher rung can get PAST a block
            # and capture the real page instead of a challenge screen, which
            # benefits a screenshot-only request too.
            and not (
                post_verdict.reason == Reason.THIN
                and not (TEXT_DEPENDENT_FORMATS & set(options.format_names))
            )
            and options.escalate
            and not options.has_captcha_checkbox
            and not fetch_req.captcha_state.get("attempted")
            and options.tier == "auto"
            and climbs < len(TIER_ORDER)
        ):
            higher = self._next_wired_tier(Tier(result.tier))
            if (
                higher is not None
                and options.maxTier is not None
                and TIER_ORDER.index(higher) > TIER_ORDER.index(options.maxTier)
            ):
                higher = None
            if higher is None:
                break
            elapsed_ms = int((time.monotonic() - request_started) * 1000)
            remaining_ms = options.timeout - elapsed_ms
            if remaining_ms < MIN_TIER_TIME_MS[higher]:
                logger.info(
                    "escalation_deadline",
                    domain=domain,
                    from_tier=result.tier,
                    wanted=str(higher),
                    remaining_ms=remaining_ms,
                )
                break
            logger.info(
                "soft_block_escalation",
                domain=domain,
                from_tier=result.tier,
                to_tier=str(higher),
                signal=post_verdict.signal,
                remaining_ms=remaining_ms,
            )
            # Correct the rung we are LEAVING before we leave it. Its row says
            # `success` because the transport succeeded — 200, bytes, a real
            # latency — and only extraction knew the body was a shell. Amending
            # only at the end left every climbed-past rung claiming success, and
            # a request that eventually succeeded amended nothing at all: the
            # log showed ancestry's http, browser and stealth rungs all
            # "succeeding" while the page they returned was worthless, which
            # sent both the pilot and me to the wrong conclusion (7 Sep 2026).
            await self._amend_attempt_log(outcome.attempts, result, post_verdict)
            climbed = await self._controller.fetch(
                dataclasses.replace(fetch_req, timeout_ms=remaining_ms),
                profile,
                forced_tier=higher,
                escalate=True,
                floor=options.tier_floor,
            )
            outcome = EscalationOutcome(
                result=climbed.result,
                verdict=climbed.verdict,
                attempts=[*outcome.attempts, *climbed.attempts],
            )
            climbs += 1
            if climbed.result is None:
                break
            result = climbed.result
            if climbed.verdict.ok:
                extraction, summary, post_verdict = await _pass(result)
                if post_verdict.ok:
                    a_thinner_rung_validated = True
                if is_richer_extraction(extraction.markdown, best_extraction.markdown):
                    best_result, best_extraction, best_summary, best_verdict = (
                        result,
                        extraction,
                        summary,
                        post_verdict,
                    )
                else:
                    logger.info(
                        "escalation_kept_richer_lower_tier",
                        domain=domain,
                        from_tier=best_result.tier,
                        to_tier=result.tier,
                        kept_words=best_extraction.word_count,
                        climbed_words=extraction.word_count,
                    )
            else:
                # The controller refused it before extraction; its verdict is
                # the one to climb on (or stop on, if it is a target error).
                post_verdict = climbed.verdict

        # Whatever the loop's own working state ended on, the actual answer
        # is the richest rung any climb produced — not necessarily the last
        # (highest) one tried.
        result, extraction, summary, post_verdict = (
            best_result,
            best_extraction,
            best_summary,
            best_verdict,
        )
        # A thinner rung already validated, so the richer one kept in its
        # place cannot be a quality regression — accept it rather than fail a
        # request the ladder would otherwise have succeeded, just worse.
        if not post_verdict.ok and post_verdict.reason == Reason.THIN and a_thinner_rung_validated:
            logger.info(
                "thin_verdict_overridden_by_richer_lower_tier",
                domain=domain,
                tier=result.tier,
                word_count=extraction.word_count,
            )
            post_verdict = replace(post_verdict, ok=True)

        # 7b. A block that only showed itself after extraction gets the same
        #     retry from another country as one the ladder caught. The retry
        #     in 5b runs straight after the ladder, and a browser rung's block
        #     page passes the ladder's first look — the challenge title is only
        #     read here. indeed.com, 22 Sep 2026: every rung refused from the
        #     caller's country, the page answered from another, and that other
        #     country was never tried because 5b had already been and gone.
        if (
            self._persist
            and not post_verdict.ok
            and not options.has_captcha_checkbox
            and not fetch_req.captcha_state.get("attempted")
            and _is_geo_retryable(post_verdict)
            and outcome.attempts
        ):
            rescued = await self._retry_other_countries(
                url, target, options, profile, domain, _geo_probe_tier(self._fetchers)
            )
            if rescued is not None and rescued.result is not None:
                r_extraction, r_summary, r_verdict = await _pass(rescued.result)
                outcome = EscalationOutcome(
                    result=rescued.result,
                    verdict=rescued.verdict,
                    attempts=[*outcome.attempts, *rescued.attempts],
                )
                if r_verdict.ok:
                    logger.info("geo_gate_rescued_after_extraction", domain=domain)
                    result, extraction, summary, post_verdict = (
                        rescued.result,
                        r_extraction,
                        r_summary,
                        r_verdict,
                    )

        if not post_verdict.ok:
            salvageable_for_non_text = post_verdict.reason == Reason.THIN and not (
                TEXT_DEPENDENT_FORMATS & set(options.format_names)
            )
            if salvageable_for_non_text:
                logger.info(
                    "thin_content_salvaged_for_non_text_formats",
                    domain=domain,
                    signal=post_verdict.signal,
                    formats=options.format_names,
                )
                post_verdict = replace(post_verdict, ok=True)
            else:
                if post_verdict.signal == "implausible_content":
                    metrics.plausibility_rejections.labels(
                        signal=",".join(post_verdict.details.get("fired", [])) or "unknown"
                    ).inc()
                await self._record_failure(profile, post_verdict, outcome.attempts, domain)
                await self._amend_attempt_log(outcome.attempts, result, post_verdict)
                raise _error_with_captcha(post_verdict, outcome.tiers_attempted, result)

        # 8. Profile update — the domain got easier or harder.
        if self._persist:
            # Learn only from rungs the engine chose. A forced rung says what
            # the caller asked for, not what the domain needs.
            chosen = options.tier == "auto"
            apply_success(profile, Tier(result.tier), extraction.char_count, chosen=chosen)
            # What this domain actually costs when it works, so the next
            # request can be given a deadline it can finish inside.
            record_success_time(profile, int((time.monotonic() - request_started) * 1000))
            # A stealth rung that had to find its own exit is the domain
            # telling us it needs one. Recording that means the NEXT request
            # routes a proxy from the start instead of burning three direct
            # attempts to rediscover it (03-fetch-tiers.md section 7).
            if chosen and endpoint is None and result.proxy_id and result.proxy_type:
                profile.requires_proxy = True
                profile.required_proxy_type = result.proxy_type
                with contextlib.suppress(Exception):
                    from engine.core.proxy import pool as proxy_pool

                    await proxy_pool.record_success(result.proxy_id, domain, result.latency_ms)
            await repo.save_domain_profile(profile, last_success=True)

        cost = Cost(
            tier=result.tier,
            tiers_attempted=outcome.tiers_attempted,
            proxy_used=result.proxy_id is not None,
            proxy_type=result.proxy_type,
            proxy_bytes=outcome.total_bytes if result.proxy_id else 0,
            browser_ms=result.browser_ms,
            extraction_path=str(extraction.extraction_path),
            cached=False,
            # A parsed document bills its pages, exactly as /v1/parse does.
            # The rate already existed; nothing on the scrape path had ever
            # set the count, because nothing on the scrape path parsed.
            pdf_pages=int(getattr(extraction, "pdf_pages", 0) or 0),
        )

        metrics.record_extraction(
            str(extraction.page_type), str(extraction.extraction_path), extraction.confidence
        )
        metrics.record_cost(result.proxy_type, cost.proxy_bytes, cost.browser_ms)
        metrics.record_escalation(len(outcome.tiers_attempted))

        data = _build_scrape_data(url, result, extraction, cost, options, post_verdict)

        # 7b. The picture, if one was asked for. `screenshot` has been an
        # accepted format, a documented one and a field on the response the
        # whole time, and NOTHING ever called the fetcher that takes it — the
        # format returned null on every request. A format we advertise and do
        # not implement is worse than one we never offered.
        if options.wants_screenshot or options.wants_network:
            await self._second_look(url, fetch_req, result, options, data, domain, job_id)

        # 8. Change tracking, when the caller asked for it. Runs before the
        # page is stored, so the comparison is against the PREVIOUS capture
        # rather than the one we are about to write.
        if options.change_tracking is not None and self._persist:
            data.changeTracking = await self._track_change(
                url, norm_hash, extraction, options, owner_ref
            )

        page_id: str | None = None
        if self._persist and options.storeInCache:
            page_id = await self._store(
                url, result, extraction, options, outcome, post_verdict, job_id, owner_ref
            )

        data = await self._deliver(
            data, options, markdown=extraction.markdown, html=extraction.html
        )

        return ScrapeOutcome(data=data, page_id=page_id, attempts=outcome.attempts)

    # -- helpers ----------------------------------------------------------

    async def _deliver(
        self,
        data: ScrapeData,
        options: ScrapeOptions,
        *,
        markdown: str | None,
        html: str | None,
    ) -> ScrapeData:
        """Fill the formats that are computed FROM the page, and explain any
        that could not be.

        One place, on both returns — the live one and the cache hit. Two of
        these were wired nowhere at all: `summary` and `json` were accepted
        formats, documented, priced as a normal fetch, offered as checkboxes
        in our own playground, and returned `null` on every request ever made.
        A cache hit would have kept doing so even after the live path was
        fixed, which is why this is a chokepoint and not two call sites.

        Anything not delivered leaves a warning naming itself. A null with no
        reason cannot be told apart from a fault, and for months it WAS one.
        """
        warnings: list[str] = []

        if options._has_format("summary"):
            if data.summary is None:
                data.summary = summarise(markdown or "")
            if not data.summary:
                warnings.append(NO_PROSE_TO_SUMMARISE)

        spec = options.json_format
        if spec is not None:
            outcome = await asyncio.to_thread(_json_against_schema, markdown or "", html, spec)
            data.json_ = outcome.data
            if outcome.error:
                warnings.append(f"json: {outcome.error}")
            if outcome.source == "model" and outcome.data is not None:
                # The same work /v1/extract charges for. Priced there and free
                # here would make the endpoint you picked decide the bill.
                data.cost.extras = {**data.cost.extras, "model_extract": 1}

        if warnings:
            data.warnings = warnings
        return data

    async def _second_look(
        self,
        url: str,
        fetch_req: FetchRequest,
        result: FetchResult,
        options: ScrapeOptions,
        data: ScrapeData,
        domain: str,
        job_id: str | None,
    ) -> None:
        """Render the page once more for what extraction cannot supply: the
        picture and the request log.

        A SECOND load, on the browser rung, because the tier that answered may
        have been plain HTTP with nothing to photograph and no scripts to fire.
        It is derived from the page's OWN request, not built fresh: the fresh
        one was `mobile=False` with no location and no exit, so every
        screenshot was the desktop page as our server saw it, whatever country
        or device the caller asked for — the entire product, for ad
        verification. Asset blocking is off, because a picture without images
        is not a picture of the page and an image pixel is how half the ad
        tags fire.

        Both artifacts share the one load, and its bytes are metered and
        written to the bandwidth ledger: going out through the caller's exit is
        what makes it right, and it is also what makes it cost.
        """
        # The log the page's own load already captured, if the rung that
        # answered could. Only what is still missing costs a second load.
        want_network = options.wants_network
        if want_network and result.network is not None:
            data.network = _network_log(result.network, result.network_seen)
            want_network = False
        if not options.wants_screenshot and not want_network:
            return

        fetcher = self._fetchers.get(Tier.BROWSER)
        req = dataclasses.replace(
            fetch_req,
            url=result.url or url,
            block_assets=False,
            timeout_ms=options.timeout,
            capture_network=False,
        )
        spec = options.screenshot_format
        shot_args = (
            (bool(spec and spec.fullPage), spec.quality if spec else None)
            if options.wants_screenshot
            else None
        )

        render = getattr(fetcher, "render", None)
        if render is None:
            # A fetcher that only takes pictures (tests, older builds). It can
            # serve the screenshot; it cannot serve a request log, and says so.
            shot = getattr(fetcher, "screenshot", None)
            # `shot_args is not None`, not `wants_screenshot`: the same fact,
            # stated in a form the type checker can follow into the index.
            if shot_args is not None and shot is not None:
                taken = await shot(req, full_page=shot_args[0], quality=shot_args[1])
                data.screenshot = taken if isinstance(taken, str) else None
            elif shot_args is not None:
                logger.warning("screenshot_unavailable", url=url)
            if want_network:
                data.warnings = [*(data.warnings or []), "network: browser rung unavailable"]
            return

        outcome = await render(req, screenshot=shot_args, network=want_network)
        if options.wants_screenshot:
            data.screenshot = outcome.screenshot
        if want_network:
            data.network = _network_log(outcome.requests, outcome.requests_seen)
        if outcome.error:
            data.warnings = [*(data.warnings or []), f"render: {outcome.error}"]

        # The bill. Only a proxied load costs vendor bytes; a direct one is ours.
        if outcome.bytes_transferred and req.proxy_id:
            data.cost.proxy_bytes += outcome.bytes_transferred
            if self._persist:
                try:
                    from engine.core.proxy import budget

                    await budget.record(
                        proxy_id=req.proxy_id,
                        domain=domain,
                        bytes_used=outcome.bytes_transferred,
                        success=outcome.error is None,
                        job_id=job_id,
                    )
                except ImportError:
                    pass
                except Exception as exc:  # noqa: BLE001 - accounting must not fail a fetch
                    _log_swallowed("second-look accounting failed", exc)

    async def _track_change(
        self,
        url: str,
        norm_hash: bytes,
        extraction: Any,
        options: ScrapeOptions,
        owner_ref: str | None = None,
    ) -> dict[str, Any] | None:
        """Compare against this account's last capture and record the new one."""
        spec = options.change_tracking
        if spec is None:
            return None

        markdown = extraction.markdown or ""
        try:
            previous_row = await repo.cache_lookup(norm_hash, max_age_ms=365 * 86_400_000)
            status, previous_at = await repo.record_version(
                norm_hash,
                url,
                repo.content_hash(change_tracking.normalise(markdown)),
                extraction.word_count,
                owner_ref,
            )
        except Exception as exc:  # noqa: BLE001 - tracking must not fail a scrape
            _log_swallowed("change tracking failed", exc)
            return None

        result = change_tracking.ChangeResult(
            status=change_tracking.ChangeStatus(status),
            previous_scrape_at=previous_at,
        )
        payload = result.to_payload()

        if status == change_tracking.ChangeStatus.CHANGED and previous_row is not None:
            previous_markdown = previous_row["markdown"] or ""
            payload.update(change_tracking.summarise(previous_markdown, markdown))
            if "git-diff" in spec.modes:
                payload["diff"] = change_tracking.git_diff(previous_markdown, markdown)
        return payload

    @staticmethod
    def _variant_country(options: ScrapeOptions) -> str:
        """What the caller asked for, not what we ended up using.

        The effective exit is only known after the fetch, but the promise being
        kept is the caller's: a row may answer a GB request only if it was
        itself fetched for one. The proxy mode rides along because the same
        country direct and through a residential exit are different documents
        often enough to matter.
        """
        country = (options.location.country if options.location else "") or ""
        return f"{country.lower()}:{options.proxy}"

    @staticmethod
    def _extraction_variant(options: ScrapeOptions) -> str:
        """The options that decide WHAT IS EXTRACTED from the fetched bytes.

        Derived from the same fields that build `ExtractOptions`, so a new
        extraction option joins the cache key by being used rather than by
        somebody remembering to add it here. Anything that only selects which
        stored field is returned — `formats` — is deliberately absent: the row
        holds markdown, html, rawHtml and links together, and picking among
        them is not a different document.
        """
        return ";".join(
            [
                f"main={int(bool(options.onlyMainContent))}",
                "inc=" + ",".join(sorted(options.includeTags)),
                "exc=" + ",".join(sorted(options.excludeTags)),
                f"b64={int(bool(options.removeBase64Images))}",
                "parsers=" + ",".join(sorted(str(p) for p in options.parsers)),
            ]
        )

    @staticmethod
    def _cache_variant(url: str, options: ScrapeOptions) -> tuple[bytes, bool]:
        """The cache key for this request, and whether it may be shared.

        A fetch carrying caller-supplied headers or cookies is personalised by
        definition — an auth token, a session, a consent state. The cache is
        shared across every customer, so those responses must never enter it:
        one caller's signed-in page becoming another caller's result is the
        worst version of a cache bug.
        """
        variant = variant_hash(
            url,
            country=ScrapeService._variant_country(options),
            mobile=bool(options.mobile),
            extraction=ScrapeService._extraction_variant(options),
            actions=ScrapeService._actions_variant(options) + ":captcha=" + options.captchaHandling,
        )
        # An interaction is a side effect and is very often personalised — a
        # form filled, a tab opened while signed in. Even with the sequence in
        # the key, one caller's post-click page must not become another's
        # answer, so a request carrying steps never enters the shared cache.
        shareable = not options.headers and not options.actions and not options.captchaEvidence
        return variant, shareable

    @staticmethod
    def _actions_variant(options: ScrapeOptions) -> str:
        """The action sequence, as a stable string for the cache key."""
        if not options.actions:
            return ""
        return json.dumps(
            [a.model_dump(exclude_none=True) for a in options.actions],
            sort_keys=True,
            separators=(",", ":"),
        )

    async def _select_proxy(
        self, domain: str, options: ScrapeOptions, profile: DomainProfile
    ) -> Any | None:
        """Pick a proxy, or None to go direct.

        Direct is the default for tiers 0 and 1 on cooperative domains: routing
        everything through a proxy reflexively costs money and gains nothing on
        a site that does not care.

        The proxy layer is an OPTIONAL capability. It is imported inside this
        method, not at module scope, so the open core runs without it: absent
        the package, every request goes direct and nothing else changes. The
        budget is checked BEFORE the fetch is dispatched — noticing an
        overspend afterwards is not a cost control.
        """
        if options.proxy == ProxyMode.NONE:
            return None

        # An EXPLICIT proxy request is a promise about which IP the target is
        # allowed to see. Every path below that cannot keep that promise
        # REFUSES rather than returning None, because None here means a direct
        # fetch from our own address — the single outcome the caller paid to
        # avoid, delivered silently and only observable after the request has
        # already gone out. AUTO keeps degrading to direct: there the proxy was
        # an optimisation we chose, not a guarantee we made.
        explicit = options.proxy != ProxyMode.AUTO
        # A COUNTRY is a promise about the exit too. `location` is documented as
        # "fetch from a particular country", and under AUTO it used to buy an
        # IP only when the domain had been learned to need one — so an ordinary
        # site asked for from the US or Japan was fetched from our own server.
        # Measured live against an IP-echo service: location US and location
        # JP both came back as the server's own country, proxy_used false. The
        # ad a page
        # serves, the SERP it ranks and the catalogue it lists are chosen by
        # the visitor's IP, not by Accept-Language, so for those products the
        # wrong exit is the wrong answer. Where we run proxies and cannot
        # supply that country we refuse; a self-hosted core with no proxy layer
        # has nothing to buy, and keeps degrading to direct as before.
        wants_place = bool(options.location and options.location.country)
        promised = explicit or wants_place
        asked = str(options.proxy) if explicit else "residential"

        def refuse(reason: str) -> None:
            logger.warning("proxy_unavailable", domain=domain, reason=reason, proxy=asked)
            raise ProxyUnavailable(reason, proxy_type=asked)

        if not self._persist:
            if explicit:
                refuse("proxy subsystem not enabled on this deployment")
            return None
        if not settings.proxy_enabled:
            if explicit:
                refuse("proxy subsystem not enabled on this deployment")
            return None

        try:
            from engine.core.proxy import budget, pool
            from engine.core.proxy.vendor import ProxyType, normalise_country
        except ImportError:
            # No proxy layer installed. Going direct is the correct degraded
            # behaviour for AUTO, and saying so once beats failing every
            # request — but it cannot answer an explicit request.
            logger.info("proxy_layer_unavailable", domain=domain)
            if explicit:
                refuse("proxy layer not installed")
            return None

        if options.proxy == ProxyMode.AUTO:
            # Only pay for an IP when the domain has shown it needs one — or
            # when the caller named a place, which only an exit can deliver.
            if not profile.requires_proxy and not wants_place:
                return None
            required = ProxyType(profile.required_proxy_type or ProxyType.RESIDENTIAL)
        else:
            required = ProxyType(str(options.proxy))

        try:
            await budget.check(required)
        except budget.BudgetExhausted as exc:
            # A clean refusal, not an exception through the worker.
            detail = str(exc.status.describe())
            logger.warning("proxy_refused_over_budget", domain=domain, detail=detail)
            if promised:
                refuse(f"proxy bandwidth budget exhausted ({detail})")
            return None

        # Precedence: what the caller asked for, then what this domain has been
        # SEEN to answer, then the configured default. The caller wins because
        # `location` is a product feature — geo-specific content — not a
        # workaround, and silently overriding it would return the wrong page.
        # Normalised here too, so the POOL and the endpoint agree on which
        # proxy this is — `pool.select` keys on the country it is given.
        country = normalise_country(
            (options.location.country if options.location else None)
            or profile.working_country
            or None
        )
        endpoint = await pool.select(
            domain,
            required,
            country=country,
            sticky=bool(options.actions),
            grade=proxy_grade(profile, options),
        )
        if endpoint is None and promised:
            # No endpoint for this (type, country), the only one is in cooldown
            # on this domain, or every provider refuses this domain. None of
            # them is a reason to send the caller's own IP to the target — but
            # they are different facts with different fixes, so say which.
            refuse(_no_exit_reason(asked, required, domain, country))
        return endpoint

    # How many extra countries to try on a blocked domain. Two, not the whole
    # list: each attempt is a paid request that probably fails, and the point
    # is to catch a geo-gate cheaply, not to tour the world.
    MAX_COUNTRY_RETRIES = 2

    async def _retry_other_countries(
        self,
        url: str,
        target: Any,
        options: ScrapeOptions,
        profile: DomainProfile,
        domain: str,
        tier: Tier,
    ) -> Any:
        """Re-attempt a blocked domain from other countries. None if unrescued.

        Probes at tier 1, not at whatever tier the original attempt reached.

        Two wrong versions preceded this. The first passed `escalate=False`,
        restarting at tier 0 — and etsy.com refuses plain httpx from every
        country. The second used the highest tier reached, which after the
        browser tier was deployed meant probing with a browser: measured,
        DataDome refuses our Chromium from a US IP while answering curl_cffi
        from the same country 10 times out of 10. The browser is MORE
        detectable here, not less, and it is also ~40x the cost.

        So the probe is deliberately cheap and deliberately the best-behaved
        fingerprint we have. If a different country is going to work at all,
        it works here.
        """
        requested = (
            options.location.country if options.location and options.location.country else None
        )
        candidates = countries_to_retry(profile, requested, self.MAX_COUNTRY_RETRIES)
        if not candidates:
            return None

        try:
            from engine.core.proxy import pool, vendor
        except ImportError:  # the open core: no exits to try another country with
            return None

        # Probe with the breaker held open. A circuit breaker keyed on the
        # DOMAIN alone will permanently lock out a domain that only refuses
        # from one country: the blocks trip the breaker, the breaker then
        # refuses the very retry that would have discovered the working
        # country, and the domain is written off for ever.
        #
        # Measured: this returned `signal=circuit_open, tiers=[]` — the retry
        # was never making a request at all.
        #
        # Bounded by MAX_COUNTRY_RETRIES, so this is a probe, not a way around
        # the breaker. It is the same intent as a half-open state.
        rule = await site_rules.effective(url, persist=self._persist)
        probing = replace(profile, circuit_open_until=None)

        for country in candidates:
            # A retry after a block is hard work by definition: premium.
            endpoint = vendor.residential(
                country=country, session=vendor.new_session_id(), domain=domain, grade="premium"
            )
            if endpoint is None:
                continue
            try:
                outcome = await self._controller.fetch(
                    FetchRequest(
                        url=url,
                        target=target,
                        timeout_ms=options.timeout,
                        # Same per-site defaults as the main attempt: a rescued
                        # geo-gate must not land on the consent wall instead.
                        headers=_with_site_cookie(dict(options.headers), rule),
                        mobile=options.mobile,
                        location=options.location,
                        block_assets=options.blockAssets and not options.wants_screenshot,
                        captcha_handling=options.captchaHandling,
                        captcha_evidence=options.captchaEvidence,
                        wait_for_ms=options.waitFor,
                        proxy_url=endpoint.connection_url(),
                        proxy_id=endpoint.scored_as,
                        proxy_type=str(endpoint.type),
                        cookies=rule.browser_cookies(),
                    ),
                    probing,
                    forced_tier=tier,  # same tier; this tests the IP, not the tier
                    escalate=False,
                )
            except Exception as exc:  # noqa: BLE001 - a retry must never fail the request
                _log_swallowed("country retry failed", exc)
                continue

            if outcome.result is not None and outcome.verdict.ok:
                logger.info("geo_gate_rescued", domain=domain, country=country)
                with contextlib.suppress(Exception):
                    await repo.record_working_country(domain, country)
                _ = pool
                return outcome

            with contextlib.suppress(Exception):
                await repo.note_country_attempt(domain, country)

        return None

    async def _note_providers(
        self, outcome: Any, domain: str, *, fallback_proxy_id: str | None = None
    ) -> None:
        """Tell provider health what each attempt's proxy actually did.

        Read from the ATTEMPTS. This used to read `outcome.result`, which is
        None on every failed request — so a rejected password, or a provider
        refusing the target, reached the health check as "no error" and the
        provider was scored as working. The failover built on 7 Sep only ever
        heard about requests that happened to return a result.
        """
        try:
            from engine.core.proxy import providers as _providers
        except ImportError:  # the open core has no providers to keep score of
            return

        heard = False
        for attempt in outcome.attempts:
            if not attempt.proxy_id:
                continue
            heard = True
            provider = _providers.provider_id_from_endpoint(attempt.proxy_id)
            if provider and _providers.refuses_target(attempt.error):
                _providers.note_refusal(provider, domain)
                try:
                    await repo.record_provider_refusal(provider, domain)
                except Exception as exc:  # noqa: BLE001 - routing already learned it
                    _log_swallowed("provider refusal not persisted", exc)
                logger.info("proxy_provider_refused_domain", provider=provider, domain=domain)
            _providers.note_result(provider, error=attempt.error)

        if not heard and fallback_proxy_id:
            result = outcome.result
            _providers.note_result(
                _providers.provider_id_from_endpoint(fallback_proxy_id),
                error=result.error if result is not None else None,
            )

    async def _retry_past_refusal(
        self,
        domain: str,
        options: ScrapeOptions,
        profile: DomainProfile,
        fetch_req: FetchRequest,
        forced: Tier | None,
    ) -> Any:
        """Run the ladder once more on the next provider. None if no better route."""
        endpoint = await self._select_proxy(domain, options, profile)
        retry_req = replace(
            fetch_req,
            proxy_url=endpoint.connection_url() if endpoint else None,
            proxy_id=endpoint.scored_as if endpoint else None,
            proxy_type=str(endpoint.type) if endpoint else None,
        )
        logger.info(
            "proxy_retry_past_refusal",
            domain=domain,
            proxy=endpoint.describe() if endpoint else "own exit",
        )
        outcome = await self._controller.fetch(
            retry_req,
            profile,
            forced_tier=forced,
            escalate=options.escalate,
            floor=options.tier_floor,
            max_tier=options.maxTier,
        )
        if endpoint is not None:
            await self._record_proxy_outcome(
                endpoint.scored_as, str(endpoint.type), domain, outcome, None
            )
        else:
            await self._note_providers(outcome, domain)
        return outcome

    async def _record_proxy_outcome(
        self,
        endpoint_id: str,
        proxy_type: str | None,
        domain: str,
        outcome: Any,
        job_id: str | None,
    ) -> None:
        """Write the bandwidth ledger and update the per-domain score."""
        try:
            from engine.core.proxy import budget, pool
        except ImportError:
            return

        result = outcome.result
        blocked = not outcome.verdict.ok and outcome.verdict.reason in (
            Reason.BLOCKED,
            Reason.SOFT_BLOCK,
        )
        try:
            await budget.record(
                proxy_id=endpoint_id,
                domain=domain,
                bytes_used=outcome.total_bytes,
                success=bool(result and outcome.verdict.ok),
                job_id=job_id,
            )
            if blocked:
                await pool.record_block(endpoint_id, domain)
            elif result is not None and outcome.verdict.ok:
                await pool.record_success(endpoint_id, domain, result.latency_ms)
            else:
                await pool.record_failure(endpoint_id, domain)

            # Provider-level health, which is a different question from this
            # endpoint's score: a 407 or a 402 says the PROVIDER is dead or
            # drained, and every endpoint it builds will fail the same way.
            # Without this the ladder keeps choosing a provider that cannot
            # answer, because priority alone has no notion of "working".
            await self._note_providers(outcome, domain, fallback_proxy_id=endpoint_id)

            retire, reason = await pool.should_retire(endpoint_id)
            if retire and reason:
                await pool.retire(endpoint_id, reason)
        except Exception as exc:  # noqa: BLE001 - accounting must not fail a fetch
            _log_swallowed("proxy accounting failed", exc)
        _ = proxy_type

    async def _acquire_politeness(
        self,
        domain: str,
        domain_delay_ms: int | None,
        domain_concurrency: int | None,
        budget: HostBudget,
    ) -> bool:
        """The plan sets the pace; the domain can only make it gentler.

        A site that answered 429 keeps its raised delay for every caller, and a
        domain we know is fragile keeps its lowered concurrency — neither can
        be bought out of.
        """
        delay_ms = max(budget.delay_ms, domain_delay_ms or 0)
        concurrency = min(budget.concurrency, domain_concurrency or budget.concurrency)

        waited = 0.0
        while waited < MAX_POLITENESS_WAIT_MS:
            decision = await self._politeness.acquire(
                domain,
                delay_ms=delay_ms,
                max_concurrency=concurrency,
                floor_delay_ms=budget.delay_ms,
                max_wait_ms=int(MAX_POLITENESS_WAIT_MS - waited),
            )
            if decision.allowed:
                # A ticket for a definite instant. Sleeping it is not a retry —
                # the turn is already ours and nobody else can take it.
                if decision.wait_ms:
                    await asyncio.sleep(decision.wait_ms / 1000)
                return True

            if decision.reason == "queue_too_long":
                return False

            # Only the concurrency cap sends us round again — the domain is at
            # its in-flight limit and a slot has to come free. Jitter so the
            # waiting callers do not all test it on the same tick.
            wait = min(decision.wait_ms or 100, MAX_POLITENESS_WAIT_MS - waited)
            wait += random.uniform(0, 60)  # noqa: S311 - retry-backoff jitter, not a security use
            await asyncio.sleep(wait / 1000)
            waited += wait
        return False

    async def _robots_for(self, host: str, url: str) -> robots_mod.RobotsRules:
        if not self._persist:
            return robots_mod.RobotsRules(fetched=False)
        cached = await repo.cached_robots(host)
        if cached is not None:
            return robots_mod.parse(cached, settings.user_agent)

        fetcher = self._fetchers.get(Tier.HTTP)
        if fetcher is None:
            return robots_mod.RobotsRules(fetched=False)
        result = await fetcher.fetch(
            FetchRequest(url=robots_mod.robots_url_for(url), timeout_ms=10_000)
        )
        if result.status_code == 200 and result.body:
            body = result.text(limit=512_000)
            await repo.store_robots(host, body)
            return robots_mod.parse(body, settings.user_agent)
        # A missing or unreachable robots.txt is not a disallow.
        await repo.store_robots(host, "")
        return robots_mod.RobotsRules(fetched=False)

    async def _link_cached_page(self, cached: Any, job_id: str, url: str) -> None:
        """Give a job its own row for a page served from the shared cache.

        Copies the stored extraction — not a refetch, not a re-extraction — so
        the crawl's results include the page at the cost of one insert. The
        unique (job_id, normalized_hash) index makes a repeat a no-op.
        """
        columns = (
            "url",
            "variant_hash",
            "shared_cacheable",
            "status_code",
            "content_type",
            "content_hash",
            "markdown",
            "html",
            "raw_html",
            "links",
            "structured",
            "title",
            "description",
            "language",
            "author",
            "published_at",
            "page_type",
            "word_count",
            "extraction_confidence",
            "extraction_path",
            "fetch_tier",
            "tiers_attempted",
            "proxy_type",
            "proxy_bytes",
            "browser_ms",
            "ok",
            "block_signals",
        )
        record = {c: cached[c] for c in columns if c in cached}
        record.update(job_id=job_id, source_url=url, normalized_hash=normalized_hash(url))
        try:
            await repo.store_page(record)
            logger.info("cached_page_linked", job_id=job_id, url=url)
        except Exception as exc:  # noqa: BLE001 - a missing link must never fail the fetch
            _log_swallowed("cached page link failed", exc)

    async def _heal_consent(
        self,
        url: str,
        fetch_req: FetchRequest,
        profile: DomainProfile,
        options: ScrapeOptions,
        rule: site_rules.SiteRule,
        domain: str,
    ) -> tuple[EscalationOutcome, site_rules.SiteRule, FetchRequest] | None:
        """Click "Accept all" over HTTP, remember the cookie, retry once.

        Returns the new outcome with the rule and request that produced it, or
        None when nothing could be harvested — in which case the caller keeps
        the failure it already had. The retry starts from the tier that met the
        wall (the rungs below it already saw the same door).
        """
        harvested = await consent.harvest(url, proxy_url=fetch_req.proxy_url)
        if harvested is None:
            return None

        if self._persist:
            with contextlib.suppress(Exception):
                await repo.save_site_cookie(domain, harvested.name, harvested.value)

        fresh = tuple(
            {**c, "value": harvested.value} if c["name"] == harvested.name else c
            for c in rule.cookies
        ) or (harvested.as_cookie(),)
        new_rule = site_rules.SiteRule(cookies=fresh, why=rule.why)

        headers = {k: v for k, v in fetch_req.headers.items() if k.lower() != "cookie"}
        if options.headers and any(k.lower() == "cookie" for k in options.headers):
            headers = dict(fetch_req.headers)  # the caller's own cookie stays theirs
        new_req = replace(
            fetch_req,
            headers=_with_site_cookie(headers, new_rule),
            cookies=new_rule.browser_cookies(),
        )

        logger.info("consent_cookie_refreshed", domain=domain, cookie=harvested.name)
        outcome = await self._controller.fetch(
            new_req,
            profile,
            forced_tier=None if options.tier == "auto" else Tier(options.tier),
            escalate=options.escalate,
            floor=options.tier_floor,
        )
        return outcome, new_rule, new_req

    def _next_wired_tier(self, tier: Tier) -> Tier | None:
        """The rung to climb to after a post-extraction soft block.

        A soft block found AFTER extraction — a JS shell, a consent wall, a
        page with no words in 12KB — means the document needed rendering. Tiers
        0 and 1 are both HTTP clients with different fingerprints; neither runs
        JavaScript, so climbing from one to the other shows the same shell
        again. Measured on Google's consent page (5 Sep 2026): http → impersonate
        → identical page → give up. The useful climb is to the first rung that
        executes JavaScript, and from there upward one rung at a time.

        Only rungs this deployment actually has: naming an absent rung is a
        no-op that looks like a decision.
        """
        floor = max(TIER_ORDER.index(tier) + 1, TIER_ORDER.index(Tier.BROWSER))
        for candidate in TIER_ORDER[floor:]:
            if candidate in self._fetchers:
                return candidate
        return None

    async def _shared_body_verdict(
        self, domain: str, url: str, markdown: str, verdict: Verdict
    ) -> Verdict:
        """Reject a body this domain has already served for other URLs.

        A site handing back its chrome instead of the page returns the SAME
        BYTES whatever you ask for. behindthename.com served an identical
        3,836-character menu bar for /name/aspen and /name/brandon; ancestry
        did the same. Neither is thin — 3.8 KB of navigation clears every
        length threshold — and neither is a block. The only thing that gives it
        away is that a DIFFERENT URL produced the SAME answer, which no
        single-page heuristic can see.

        THIN, deliberately, not SOFT_BLOCK: the target did not refuse us, so
        this must climb without raising the domain's tier floor. A verdict
        about a PAGE that writes itself into shared state taxes every other
        URL on the host.

        Three distinct URLs, not two: two identical bodies is a coincidence a
        real site produces (two redirects landing together, two empty result
        pages), while three is not. In a batch the third arrives seconds later,
        so the threshold costs nothing in practice.

        CORROBORATED, because "the same body at three URLs" means two things.
        It means chrome — and it also means a site that legitimately serves one
        page whatever query string you add, which is most sites with tracking
        parameters. example.com answered four `?probe=` variants with its real
        page and was failed as a decoy across all four tiers.

        The tell is what the shared body IS. behindthename's was a 3,836-char
        menu bar: a nav shell on every shape test. example.com's is three lines
        of prose that ends a sentence. Sharing plus chrome is a decoy; sharing
        alone is a canonical URL.
        """
        if not markdown or not self._persist:
            return verdict
        try:
            shared_by = await repo.record_content_fingerprint(
                domain, repo.content_hash(markdown), normalized_hash(url)
            )
        except Exception:  # noqa: BLE001 - a diagnostic must never fail a scrape
            logger.warning("fingerprint_check_failed", domain=domain, exc_info=True)
            return verdict
        if shared_by < SHARED_BODY_MIN_URLS:
            return verdict

        from engine.core.detect.validator import (
            NAV_SHELL_MAX_SENTENCE_SHARE,
            _shape,
        )

        # Chrome has no sentences. Measured across four shapes: a menu as one
        # long line and a menu as 120 short lines both score 0.00, while
        # example.com's real page scores 0.33 and an article 1.00. The nav
        # shell test alone missed the single-line menu, since that is one line
        # and a shell needs eight.
        if _shape(markdown)["sentences"] > NAV_SHELL_MAX_SENTENCE_SHARE:
            # Shared, but it is a real page. A site answering every query
            # variant with the same canonical content is not a decoy.
            logger.info(
                "shared_body_is_real_content",
                domain=domain,
                url=url,
                shared_by=shared_by,
                chars=len(markdown),
            )
            return verdict

        logger.info(
            "shared_body_detected",
            domain=domain,
            url=url,
            shared_by=shared_by,
            chars=len(markdown),
        )
        return Verdict(
            ok=False,
            reason=Reason.THIN,
            signal="shared_body",
            confidence=0.9,
            details={"shared_by_urls": shared_by, "chars": len(markdown)},
        )

    async def _record_failure(
        self,
        profile: DomainProfile,
        verdict: Verdict,
        attempts: list[Attempt],
        domain: str,
    ) -> None:
        if not self._persist:
            return
        # Both of our own refusals: the url's backoff is one too. Missing it
        # here meant every visitor refused during a url's backoff re-armed it
        # for a fresh fifteen minutes, so a page people kept trying never came
        # back (22 Sep 2026, the homepage's own example).
        if verdict.signal in ("circuit_open", "url_backoff_open"):
            # Our own refusal. The domain was never contacted, so there is
            # nothing here to learn from — and recording it anyway was a bug
            # measured live: nine refused requests each found the window
            # still full of the SAME failures, counted themselves as nine
            # more openings, and took a fresh 15-minute breaker straight to
            # the 24-hour cap in under ten seconds.
            return
        if verdict.reason in (Reason.BLOCKED, Reason.SOFT_BLOCK) and attempts:
            apply_block(profile, attempts[0].tier, verdict.vendor)
        else:
            profile.failure_count += 1

        by_url = await repo.recent_domain_outcomes_by_url(domain)
        recent = [ok for _, ok in by_url]
        if should_open_circuit(recent):
            # WHOSE fault is this window? Failures spread across the host are a
            # host problem and the domain breaker is right. Failures that are
            # all the same url are a PAGE problem, and shutting the domain for
            # them refuses everything else on it — google.com/privacy came back
            # ENGINE_REFUSED because a search url had failed (20 Sep 2026).
            failing = {h for h, ok in by_url if not ok and h}
            if len(failing) == 1:
                minutes = circuit_backoff_minutes(profile.circuit_opens + 1)
                await url_backoff.note_failure(domain, next(iter(failing)), minutes)
                logger.info(
                    "one_url_backed_off_instead_of_the_domain",
                    domain=domain,
                    minutes=min(minutes, url_backoff.MAX_MINUTES),
                )
                await repo.save_domain_profile(
                    profile, last_block=verdict.reason in (Reason.BLOCKED, Reason.SOFT_BLOCK)
                )
                return

            # Longer each time it reopens without a success in between. The
            # breaker was a flat 15 minutes, and a domain that has never worked
            # at any rung paid for a full-ladder retry every time it closed.
            profile.circuit_opens += 1
            profile.circuit_open_until = (
                datetime.now(UTC)
                + timedelta(minutes=circuit_backoff_minutes(profile.circuit_opens))
            ).timestamp()
            logger.info(
                "circuit_opened",
                domain=domain,
                consecutive=profile.circuit_opens,
                minutes=circuit_backoff_minutes(profile.circuit_opens),
            )

        await repo.save_domain_profile(
            profile, last_block=verdict.reason in (Reason.BLOCKED, Reason.SOFT_BLOCK)
        )

    async def _store(
        self,
        url: str,
        result: FetchResult,
        extraction: Any,
        options: ScrapeOptions,
        outcome: Any,
        verdict: Verdict,
        job_id: str | None,
        owner_ref: str | None = None,
    ) -> str:
        record: dict[str, Any] = {
            "job_id": job_id,
            "url": result.url,
            "source_url": url,
            "normalized_hash": normalized_hash(url),
            "variant_hash": self._cache_variant(url, options)[0],
            "shared_cacheable": (
                self._cache_variant(url, options)[1]
                and not (result.action_results or {}).get("captcha")
            ),
            # Whose spend paid for this row. The OWNER, not the key: a
            # customer's second key must still read their own rows free.
            "fetched_by": owner_ref,
            "status_code": result.status_code,
            "content_type": result.content_type,
            "content_hash": repo.content_hash(extraction.markdown),
            "markdown": extraction.markdown,
            "html": extraction.html if options._has_format("html") else None,
            "raw_html": result.text() if options._has_format("rawHtml") else None,
            "links": extraction.links,
            "structured": extraction.structured,
            "title": extraction.title,
            "description": extraction.description,
            "language": extraction.language,
            "author": extraction.author,
            "page_type": str(extraction.page_type),
            "word_count": extraction.word_count,
            "extraction_confidence": extraction.confidence,
            "extraction_path": str(extraction.extraction_path),
            "fetch_tier": result.tier,
            "tiers_attempted": outcome.tiers_attempted,
            "proxy_type": result.proxy_type,
            "proxy_bytes": outcome.total_bytes if result.proxy_id else 0,
            "browser_ms": result.browser_ms,
            "ok": True,
            # Near-miss data is recorded even on success so thresholds can be
            # tuned later without re-crawling.
            "block_signals": verdict.details or None,
        }
        page_id = await repo.store_page(record)

        # Fold this page's outbound links into the domain graph. The links
        # were already being extracted and stored; nothing read them across
        # pages, so "who links to this domain" — the one question they answer
        # — could not be asked at all. Never allowed to fail a scrape: it is
        # derived data, and `tools/backfill_link_graph.py` can rebuild it.
        if extraction.links:
            try:
                await repo.record_links(result.url or url, list(extraction.links))
            except Exception as exc:  # noqa: BLE001
                _log_swallowed("link graph write failed", exc)

        return page_id


# --------------------------------------------------------------------------
# Mapping helpers
# --------------------------------------------------------------------------


def _outcome_for(verdict: Verdict) -> str:
    if verdict.reason == Reason.TARGET_ERROR:
        return "target_error"
    if verdict.reason in (Reason.BLOCKED, Reason.SOFT_BLOCK):
        return "blocked"
    if verdict.signal == "transport_error":
        return "timeout"
    if verdict.reason == Reason.THIN:
        # Its own outcome, not "error". The fetch worked perfectly; the PAGE
        # was a shell. Logging it as an error sends whoever is auditing the
        # domain looking for a transport fault that never happened. Every
        # consumer compares against 'success', so this reads as a failure
        # exactly as before.
        return "thin"
    return "error"


# A block worth retrying from another country. Checked against the verdict's
# `reason`, which is the stable field — the first version of this guard looked
# for a status code in `details` and for signal names like "waf_block", and
# neither exists: a real DataDome 403 arrives as
# reason="BLOCKED", signal="datadome", details={}. So it never fired.
#
# BLOCKED and SOFT_BLOCK are refusals a different IP might change. TARGET_ERROR
# is the page not being there, and no passport fixes that.
_GEO_RETRYABLE_REASONS = frozenset({Reason.BLOCKED, Reason.SOFT_BLOCK})


# A domain is budget work only on its record: enough clean successes, no block
# of any kind, no firewall seen, and a working rung that is a plain fetch.
BUDGET_MIN_SUCCESSES = 3
_BUDGET_TIERS = frozenset({Tier.HTTP, Tier.IMPERSONATE})


def proxy_grade(profile: DomainProfile, options: ScrapeOptions) -> str:
    """Which grade of proxy provider this request should go out through.

    Budget providers cost a fraction of premium ones per GB and, measured on a
    defended job board, fail where premium ones get through. So budget is kept
    for work that is cheap by evidence — a domain this engine has already
    fetched cleanly at the plain rungs, several times, without one block —
    and everything else is premium: a domain seen for the first time, one
    that has ever blocked, one behind a known firewall, a browser-rung domain,
    and a request that clicks or pretends to be a phone. A budget exit that
    gets blocked puts a block on the record, and the domain is premium from
    the next request on.
    """
    easy = (
        profile.success_count >= BUDGET_MIN_SUCCESSES
        and profile.block_count == 0
        and profile.detected_waf is None
        and profile.min_working_tier in _BUDGET_TIERS
        and not options.actions
        and not options.mobile
    )
    return "budget" if easy else "premium"


def countries_to_retry(profile: DomainProfile, requested: str | None, limit: int) -> list[str]:
    """Which countries to try again from, after the first attempt was refused.

    "Known" does not mean "already tried": a caller who asked for the US, on a
    domain known to answer only from GB, was refused from the US on every rung
    and then never tried from GB at all, because this returned nothing whenever
    a working country was on record. A repeat of the known country is a fresh
    exit each time, not the same one twice.
    """
    asked = requested.lower() if requested else None
    if profile.working_country:
        # Always the known country, from FRESH exits, even when the caller named
        # it. The first attempt may never have used it: a domain that does not
        # require a proxy goes out direct, and indeed.com's first two rungs left
        # from our own address, which Cloudflare refuses (22 Sep 2026). And one
        # refused exit says little about the next: the same provider in the
        # same country answered indeed.com 2 times in 4 at tier 1. Each retry
        # is a new session, so a new IP, at the cheap tier.
        return [profile.working_country.lower()] * limit
    already = {c.lower() for c in profile.country_attempts}
    if asked:
        already.add(asked)
    return [c for c in repo.COUNTRY_FALLBACKS if c not in already][:limit]


def _with_site_cookie(headers: dict[str, str], rule: site_rules.SiteRule) -> dict[str, str]:
    """Fold the site's default cookies into the HTTP headers for tiers 0/1.

    A caller's own Cookie header always wins: they know something about the
    session that we do not, and merging two Cookie headers is how you send a
    consent cookie to a logged-in account.
    """
    site_cookie = rule.cookie_header()
    if site_cookie and not any(k.lower() == "cookie" for k in headers):
        headers["Cookie"] = site_cookie
    return headers


def _looks_like_consent(outcome: EscalationOutcome) -> bool:
    """Did the ladder end on a consent wall rather than a bot defence?

    Three ways it shows: the fetch was redirected onto a consent host
    (`challenge_redirect`), the final URL is one, or the page rendered as a 200
    whose title or body said so.
    """
    verdict = outcome.verdict
    if verdict.signal in ("challenge_title", "consent_wall"):
        return True
    if verdict.signal == "challenge_redirect":
        host = str(verdict.details.get("host", ""))
        return consent.is_consent_redirect(f"https://{host}/")
    return outcome.result is not None and consent.is_consent_redirect(outcome.result.url)


def _geo_probe_tier(fetchers: dict[Tier, Fetcher]) -> Tier:
    """The tier to probe a suspected geo-gate with.

    Tier 1: cheapest fingerprint that real sites accept, and measurably less
    detectable than the browser on at least one major WAF.
    """
    return Tier.IMPERSONATE if Tier.IMPERSONATE in fetchers else Tier.HTTP


def _is_geo_retryable(verdict: Verdict) -> bool:
    return verdict.reason in _GEO_RETRYABLE_REASONS


def _error_with_captcha(
    verdict: Verdict,
    tiers: list[str],
    result: FetchResult | None,
) -> EngineError:
    error = _error_for(verdict, tiers)
    if result is not None and result.action_results and result.action_results.get("captcha"):
        error.detail["actions"] = {"captcha": result.action_results["captcha"]}
    return error


def _error_for(verdict: Verdict, tiers: list[str]) -> EngineError:
    if verdict.signal == "action_failed":
        from engine.core.errors import ExtractionFailed, InvalidRequest

        message = str(verdict.details.get("error") or "An action could not be performed")
        detail = {"tiers_attempted": tiers, "fault": verdict.details.get("fault", "invalid")}
        if verdict.details.get("actions"):
            detail["actions"] = verdict.details["actions"]
        # Reported as FETCH_FAILED ("Network-level failure fetching the
        # target") this sent someone to look at their connection for a step of
        # their own. But the two kinds are not one answer: a selector that
        # will not parse is theirs to fix, and a well-formed one the page did
        # not answer may mean the page changed, never finished loading, or
        # showed something other than what was expected. Telling the second
        # caller to fix their syntax sends them after the wrong thing.
        if verdict.details.get("fault") == "challenge":
            error = Blocked(message, tiers_attempted=tiers, final_signal="captcha_unresolved")
            error.detail.update(detail)
            return error
        if verdict.details.get("fault") == "timeout":
            return ExtractionFailed(
                message + ". The step is well-formed, so the page may have changed, "
                "not finished loading, or shown something other than the page expected.",
                detail,
            )
        return InvalidRequest(message, detail)
    if verdict.reason == Reason.TARGET_ERROR:
        return TargetError(verdict.details.get("status_code"))
    if verdict.reason == Reason.ROBOTS_DENIED:
        return RobotsDenied(verdict.details.get("url", ""))
    if verdict.reason == Reason.EMPTY and verdict.signal == "transport_error":
        from engine.core.errors import FetchFailed

        return FetchFailed(
            "Network-level failure fetching the target",
            {"tiers_attempted": tiers, "error": verdict.details.get("error")},
        )
    if verdict.reason == Reason.THIN:
        from engine.core.errors import ExtractionFailed

        # Not a block and never charged: the page answered, and there was
        # nothing in it. Saying BLOCKED here sent people chasing the wrong cause.
        return ExtractionFailed(
            "The page came back but nothing usable could be read from it",
            {"tiers_attempted": tiers, "signal": verdict.signal, **verdict.details},
        )
    # Ours, not theirs. `circuit_open` printing "Target returned a challenge"
    # cost a colleague an afternoon chasing IMDb for our own breaker.
    if verdict.signal in ENGINE_SIDE_SIGNALS:
        from engine.settings import settings

        # `minutes` is a "come back later" hint, and only circuit_open has a
        # later to come back to. Every other engine-side signal used to get
        # settings.circuit_open_minutes anyway — a `deadline_exceeded` from a
        # caller's own 60-second timeout came back reporting "minutes: 15",
        # the circuit breaker's cooldown, wholly unrelated to a timeout that
        # trips instantly on every retry. Reported live: it read as "retry in
        # 15 minutes" next to a message that actually says raise `timeout`.
        if verdict.signal in ("circuit_open", "url_backoff_open"):
            retry_after = verdict.details.get("retry_after_s") if verdict.details else None
            minutes = (
                max(1, -(-int(retry_after) // 60))
                if retry_after is not None
                else settings.circuit_open_minutes
            )
            return EngineRefused(verdict.signal, tiers_attempted=tiers, minutes=minutes)
        return EngineRefused(verdict.signal, tiers_attempted=tiers)

    return Blocked(
        "Target returned a challenge or non-content page at all attempted tiers",
        tiers_attempted=tiers,
        final_signal=verdict.signal,
    )


def _verbatim_body(res: FetchResult, stats: Any) -> tuple[Any, ExtractionSummary, Verdict] | None:
    """A body that is already text, returned exactly as the server sent it.

    JSON, plain text and markdown went through the HTML-to-markdown converter,
    which escapes backslashes, asterisks and underscores. A JSON response came
    back with every `\\"` doubled to `\\\\"` and would not parse (Wiktionary's
    API, found by a dictionary-building run, 23 Sep 2026), and a word list
    would have lost its underscores the same way. None of these is a page, so
    none of them needs converting.
    """
    from engine.core.detect.validator import (
        is_plain_text,
        is_structured_data,
        looks_like_json,
    )

    body = res.body or b""
    if not body:
        return None
    ctype = (res.content_type or "").split(";", 1)[0].strip().lower()
    text_like = is_plain_text(res.content_type) or ctype == "text/markdown"
    is_json = looks_like_json(body) and (
        is_structured_data(res.content_type) or text_like or not ctype
    )
    if not (is_json or text_like):
        return None
    text = res.text()
    head = text[:1024].lstrip().lower()
    if not is_json and (head.startswith(("<!doctype html", "<html")) or "<html" in head):
        # Mislabelled HTML: a page after all, so it takes the page path.
        return None

    words = len(text.split())
    summary = ExtractionSummary(
        markdown=text,
        page_type="document",
        external_link_count=0,
        has_images=False,
        has_author=False,
        word_count=words,
        char_count=len(text),
        confidence=1.0,
    )
    extraction = ExtractionResult(
        markdown=text,
        page_type=PageType.UNKNOWN,
        extraction_path=ExtractionPath.VERBATIM,
        word_count=words,
        confidence=1.0,
    )
    return extraction, summary, validate(res, stats, summary)


def _parse_document(
    res: FetchResult, options: ScrapeOptions
) -> tuple[Any, ExtractionSummary, Verdict] | None:
    """Read a fetched document with the parser, or None if it is a web page.

    Gated on `options.parsers`, which until now existed only in the cache key:
    a caller switching the pdf parser off got PDF parsing anyway and a
    different cache entry for their trouble.
    """
    from engine.core.parse import document_kind, parse_bytes

    body = res.body or b""
    if not body:
        return None

    # `document_kind`, not `kind_of`: on this path "not a document" is the
    # ordinary answer for most of the web, and `kind_of` RAISES on it. Asking
    # the raising question here 500'd every JSON API — the body came back
    # fine, and the response was an INTERNAL error with a trace id.
    kind = document_kind(res.url or "", res.content_type or "")
    # html and text are the extractor's job; it does far more with them.
    if kind not in {"pdf", "docx"} or kind not in {str(p) for p in options.parsers}:
        return None

    try:
        doc = parse_bytes(res.url or "document", res.content_type or "", body)
    except Exception as exc:  # noqa: BLE001 - a document we cannot read is still a page
        _log_swallowed("document parse failed", exc)
        return None

    words = len(doc.markdown.split())
    summary = ExtractionSummary(
        markdown=doc.markdown,
        page_type="document",
        external_link_count=0,
        has_images=False,
        has_author=False,
        word_count=words,
        char_count=len(doc.markdown),
    )
    verdict = (
        Verdict(ok=True)
        if words
        else Verdict(ok=False, reason=Reason.THIN, signal="empty_document", confidence=1.0)
    )

    # The real ExtractionResult, not a stand-in: a SimpleNamespace here was
    # missing char_count and took down every PDF scrape with an
    # AttributeError. The dataclass cannot be missing a field the rest of the
    # pipeline reads.
    extraction = ExtractionResult(
        markdown=doc.markdown,
        page_type=PageType.UNKNOWN,
        extraction_path=ExtractionPath.PARSER,
        word_count=words,
        confidence=1.0,
    )
    extraction.pdf_pages = doc.pages  # type: ignore[attr-defined]
    return extraction, summary, verdict


def _build_scrape_data(
    source_url: str,
    result: FetchResult,
    extraction: Any,
    cost: Cost,
    options: ScrapeOptions,
    verdict: Verdict,
) -> ScrapeData:
    _ = verdict
    return ScrapeData(
        # What the caller's steps produced. The field has been in the response
        # model since the API was frozen and was never once populated.
        actions=(
            ActionResults(
                screenshots=result.action_results.get("screenshots", []),
                scrapes=result.action_results.get("scrapes", []),
                javascriptReturns=result.action_results.get("javascriptReturns", []),
                captcha=result.action_results.get("captcha", []),
            )
            if result.action_results
            else None
        ),
        markdown=extraction.markdown if options._has_format("markdown") else None,
        html=extraction.html if options._has_format("html") else None,
        rawHtml=result.text() if options._has_format("rawHtml") else None,
        links=extraction.links if options._has_format("links") else None,
        media=(
            [MediaAsset(url=m.url, type=m.type, alt=m.alt) for m in extraction.media]
            if options._has_format("media")
            else None
        ),
        metadata=PageMetadata(
            title=extraction.title,
            description=extraction.description,
            language=extraction.language,
            author=extraction.author,
            publishedAt=extraction.published_at,
            sourceURL=source_url,
            url=result.url,
            statusCode=result.status_code,
            contentType=result.content_type,
            pageType=str(extraction.page_type),
            wordCount=extraction.word_count,
            extractionConfidence=extraction.confidence,
            platform=_platform_of(result.text(limit=400_000), result.url),
        ),
        cost=cost,
    )


NO_PROSE_TO_SUMMARISE = (
    "summary: this page has no continuous prose to summarise — a listing, a "
    "directory or a form has nothing to condense."
)


def _row_html(row: Any) -> str | None:
    """The best HTML a stored row can offer.

    Raw first: it is the document as the server sent it, so the <script
    type="application/ld+json"> blocks and the <img> tags are both still
    there. One function, because media collection and schema extraction both
    need "the HTML behind this row" and answering that question two ways is
    how one of them ends up reading a column the other does not.
    """
    for column in ("raw_html", "html"):
        value = row.get(column, None)
        if value:
            return str(value)
    return None


def _json_against_schema(markdown: str, html: str | None, spec: Any) -> Any:
    """The `json` format, through the SAME function /v1/extract uses.

    Structured markup answers most schemas for free; a model is only asked
    when the schema is still unsatisfied, and the answer is validated against
    the schema before it is returned either way. Synchronous — the caller runs
    it in a worker thread, because a model call on the event loop stalls every
    other request on the process.

    JSON-LD is read from the EXTRACTION's html, never from `data.html`. Off
    `data.html` the caller's choice of formats would silently decide whether
    structured markup was consulted at all: asking for markdown alone would
    quietly make the free path unavailable and push every request onto a paid
    model call.
    """
    from selectolax.parser import HTMLParser

    from engine.core.extract.classify import parse_json_ld
    from engine.core.extract.model import model_caller
    from engine.core.extract.structured_json import extract_against_schema

    hints: dict[str, Any] | None = None
    if html:
        try:
            blocks = parse_json_ld(HTMLParser(html))
        except Exception as exc:  # noqa: BLE001 - malformed markup is not a failure
            _log_swallowed("json-ld parse failed", exc)
            blocks = []
        if blocks:
            hints = blocks[0] if len(blocks) == 1 else {"@graph": blocks}

    return extract_against_schema(
        markdown=markdown,
        structured_hints=hints,
        schema=spec.effective_schema,
        prompt=spec.prompt,
        model=model_caller(),
    )


def _no_exit_reason(asked: str, required: Any, domain: str, country: str | None) -> str:
    """Why an explicit proxy request got no exit — the real reason, not a shrug.

    "No residential exit available" read as "nothing is configured" when both
    providers were configured and healthy and had simply each refused this one
    site (www.gov.uk, 11 Sep 2026). A caller can only act on the fact they are
    told, and the fix for a refusal — fetch it directly — is not the fix for a
    missing provider.
    """
    where = f" in {country.upper()}" if country else ""
    plain = f"no {asked} exit available{where}"
    try:
        from engine.core.proxy import providers as _providers
    except ImportError:
        return plain
    if _providers.candidates(required) and not _providers.candidates(required, domain):
        return (
            f"every {asked} provider refuses to carry {domain}; "
            'send proxy "auto" or "none" to fetch it directly'
        )
    return plain


def _hit_a_refusal(outcome: Any) -> bool:
    """Did any attempt meet a provider refusing to carry this target?"""
    try:
        from engine.core.proxy import providers as _providers
    except ImportError:  # no providers, so none refused
        return False

    return any(_providers.refuses_target(a.error) for a in outcome.attempts)


def _platform_of(html: str | None, url: str = "") -> str | None:
    """Which platform built the page, when the proprietary module is present.

    A function-scope import behind ImportError is the open-core pattern: the
    public core must run without engine.platforms and simply report None. The
    URL matters too: Amazon is only ever known by its hostname.
    """
    if not html and not url:
        return None
    try:
        from engine.platforms.detect import detect_platform_for_url
    except ImportError:
        return None
    try:
        found = detect_platform_for_url(url, (html or "")[:400_000], {})
    except Exception:  # noqa: BLE001 - detection must never fail a scrape
        return None
    return str(found) if found else None


def page_metadata_from_row(row: Any, source_url: str) -> PageMetadata:
    """A stored page's metadata, in the one shape every endpoint hands back.

    `/v1/scrape` and the crawl/batch page listings used to build this
    separately, and the URL ended up at `metadata.sourceURL` on one and `url`
    on the other (measured, Sep 2026). One function, one shape.
    """
    published = row["published_at"]
    return PageMetadata(
        title=row["title"],
        description=row["description"],
        language=row["language"],
        author=row["author"],
        publishedAt=published.isoformat() if hasattr(published, "isoformat") else published,
        sourceURL=source_url,
        url=row["url"],
        statusCode=row["status_code"],
        contentType=row["content_type"],
        pageType=row["page_type"] or "unknown",
        wordCount=row["word_count"] or 0,
        extractionConfidence=row["extraction_confidence"] or 0.0,
        # asyncpg.Record has no .get(); `in` on a Record checks its keys.
        platform=_platform_of(row["html"] if "html" in row else None, row["url"]),  # noqa: SIM401
    )


# The content column behind each requested format. `links` is stored for every
# fetch; the two HTML columns are written only when asked for, which is what
# makes a stored row narrower than the next caller's request.
_FORMAT_COLUMNS = {"markdown": "markdown", "html": "html", "rawHtml": "raw_html", "links": "links"}

# Rungs that actually run a browser. A row stored by anything else cannot
# satisfy a request that needs one.
_BROWSER_TIERS = frozenset({"browser", "stealth", "stealth_hard", "mobile"})


def _row_is_thin(row: Any) -> bool:
    """Would today's validator refuse this stored page? Then it is not a hit."""
    from engine.core.detect.validator import is_nav_shell

    words = int(row["word_count"] or 0)
    if words == 0:
        return True
    return is_nav_shell(row["markdown"] or "", words, str(row["extraction_path"] or ""))


def _row_satisfies(row: Any, options: ScrapeOptions) -> bool:
    """Can this stored row answer the formats being asked for?

    `_extraction_variant` deliberately leaves `formats` out of the cache key,
    on the stated grounds that "the row holds markdown, html, rawHtml and links
    together". It does not: `html` and `raw_html` are written only when the
    storing caller requested them. So a markdown-only scrape leaves a row that
    the key says is valid for an `html` request and that cannot answer one.

    Keeping formats out of the key is still right — it would fragment the cache
    for what is usually the same document. Checking the row can actually answer
    is the cheaper half: a hit stays a hit whenever the columns are there.
    """

    # A request carrying `actions`, `waitFor` or a screenshot cannot be
    # answered by a body no browser produced. The cache key does not separate
    # them — `_cache_variant` covers url, country, mobile and extraction — so
    # without this a caller who asked for five seconds of settling, or for
    # clicks, was handed a plain HTTP row and BILLED AT BROWSER RATES for it.
    # Reported from a real harvest, 8 Sep 2026, and confirmed in the source.
    #
    # Checked here rather than added to the key, deliberately: the reverse
    # direction is fine. A cheap request may reuse a browser-rendered row —
    # it is strictly richer — and putting `forces_browser` in the key would
    # fragment the cache for no gain.
    def _column(name: str) -> Any:
        # Rows arrive as asyncpg Records here and as plain dicts in tests; a
        # column the caller's build predates must not raise.
        return row.get(name, None)

    if options.forces_browser and str(_column("fetch_tier") or "") not in _BROWSER_TIERS:
        return False
    if options.wants_screenshot and not _column("screenshot_path"):
        return False
    # A request log is an OBSERVATION of one visit — which tags fired, now,
    # from this country. There is no column for it and there should not be:
    # yesterday's log answers "did the pixel fire?" with yesterday's answer.
    # Without this the format loop skipped the unmapped name and a cached row
    # satisfied the request with `network: null`.
    if options.wants_network:
        return False
    # `media` has no column of its own — it is a projection of the stored
    # HTML, recomputed on read so an improvement to the collector reaches old
    # rows too. But a row with no HTML cannot answer it, and the format loop
    # below skips anything unmapped, so without this a cached row satisfied a
    # media request and returned an empty list.
    if options._has_format("media") and not _column("raw_html") and not _column("html"):
        return False

    for requested in options.formats:
        column = _FORMAT_COLUMNS.get(str(requested))
        if column is None:
            continue  # a format not backed by a stored column, e.g. screenshot
        if column not in row or row[column] is None:
            return False
    return True


def formats_from_page_row(row: Any, options: ScrapeOptions) -> dict[str, Any]:
    """The requested content formats out of a stored page row, and only those.

    Shared with the crawl/batch `/pages` endpoints, which returned `markdown`
    and nothing else — so a job that asked for `html`, `rawHtml` or `links` had
    the work done, stored and billed, then silently not handed back. One mapping
    for both surfaces, so they cannot answer the same question differently.
    """
    out: dict[str, Any] = {
        field: row[column]
        for field, column in _FORMAT_COLUMNS.items()
        if options._has_format(field) and row[column] is not None
    }

    if options._has_format("summary") and "markdown" in row:
        # Computed, not stored: there is no summary column, and adding one
        # would date the moment the summariser changed. The crawl and batch
        # `/pages` listings read this function too, so a job that asked for
        # `summary` gets it there as well — the alternative is the format
        # working on one surface and returning null on the other.
        summary = summarise(row["markdown"] or "")
        if summary:
            out["summary"] = summary

    if options._has_format("media"):
        source = _row_html(row)
        if source:
            base = (row.get("url", None)) or ""
            out["media"] = [
                MediaAsset(url=m.url, type=m.type, alt=m.alt) for m in collect_media(source, base)
            ]

    return out


def _data_from_page_row(
    row: Any, source_url: str, options: ScrapeOptions, owner_ref: str | None = None
) -> ScrapeData:
    """A cache hit does no fetching; whether it costs anything depends on
    whose spend put the row there."""
    stored_by = row.get("fetched_by", None)
    # An unowned row — stored before attribution existed, or by a monitor or
    # warm-up with no owner — counts as somebody else's. That charges rather
    # than gives away, which is the safe direction, and it corrects itself as
    # rows age out of the window.
    own = bool(owner_ref) and stored_by == owner_ref

    return ScrapeData(
        **formats_from_page_row(row, options),
        metadata=page_metadata_from_row(row, source_url),
        cost=Cost.from_cache(
            tier=row["fetch_tier"],
            tiers_attempted=list(row["tiers_attempted"] or []),
            extraction_path=row["extraction_path"],
            cache_own=own,
        ),
    )


def _external_links(links: list[str] | None, page_url: str) -> int:
    """Outbound links to other sites.

    A decoy page is typically self-contained: generated filler rarely links
    anywhere real, so the absence of outbound links is one of the incidental
    markers layer 4 looks for.
    """
    if not links:
        return 0
    from engine.core.urls import registrable_domain

    own = registrable_domain(page_url)
    return sum(1 for link in links if registrable_domain(link) != own)


# The request list is capped; the tracker summary is not (it is a handful of
# rows, and it is the part a caller verifying tags actually reads).
NETWORK_REQUEST_CAP = 500


def _network_log(entries: list[dict[str, Any]], seen: int) -> NetworkLog:
    """The `network` format: recognised tags first, then the raw requests.

    POST bodies are read by the tracker detector — TikTok puts its pixel code
    and event there — and dropped before anything is returned. A pixel can
    carry hashed emails from advanced matching and a beacon can carry what a
    visitor typed; neither is ours to hand on.
    """
    from engine.core.extract import trackers

    hits = [TrackerHit(**hit) for hit in trackers.detect(entries)]
    kept = entries[:NETWORK_REQUEST_CAP]
    return NetworkLog(
        trackers=hits,
        requests=[
            NetworkRequest(**{k: v for k, v in entry.items() if not k.startswith("_")})
            for entry in kept
        ],
        total=max(seen, len(entries)),
        truncated=max(seen, len(entries)) > len(kept),
    )


def _log_swallowed(message: str, exc: Exception) -> None:
    import structlog

    structlog.get_logger(__name__).warning(message, error=str(exc), error_type=type(exc).__name__)


_FEED_SUFFIXES = (".rss", ".atom", ".xml")
_FEED_SEGMENTS = frozenset({"feed", "rss", "atom"})


def _is_feed_url(url: str) -> bool:
    """A URL whose answer is a feed or other raw XML document.

    Decided from the URL alone, before any fetch, because it chooses the ladder:
    `/r/x/.rss`, `/feed`, `sitemap.xml`, `?format=rss`. A miss costs nothing but
    the old behaviour; a false positive only caps a page at the HTTP tiers.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    path = parts.path.lower().rstrip("/")
    if path.endswith(_FEED_SUFFIXES):
        return True
    if path.rsplit("/", 1)[-1] in _FEED_SEGMENTS:
        return True
    query = parts.query.lower()
    return any(f"{k}={v}" in query for k in ("format", "feed") for v in ("rss", "atom"))


def normalize_for_cache(url: str) -> str:
    return normalize_url(url)


__all__ = [
    "ScrapeService",
    "ScrapeOutcome",
    "normalize_for_cache",
    "url_hash",
    "DomainStats",
]
