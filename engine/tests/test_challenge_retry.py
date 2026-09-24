"""A challenge at the deepest rung is a coin toss, so ask again.

Measured on g2.com, 7 Sep 2026, one fetch per fresh residential session, same
rung and same minute:

    stealth_hard, /categories/crm        4 of 6 returned the real page
    stealth_hard, /products/*/reviews    1 of 5

The variable is which exit IP the session landed on. Climbing cannot help —
nothing sits above these rungs — and returning BLOCKED throws away a two-in-
three chance on the pages that matter. Every fetch mints a new proxy session,
so asking again IS a new IP.

Deliberately narrow: only the rungs where a pass is actually possible. Retrying
a challenge at tier 0 buys nothing but latency, because tier 0 never passes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.core.fetch.base import FetchRequest, FetchResult
from engine.core.fetch.escalation import (
    CHALLENGE_RETRIES,
    DomainProfile,
    EscalationController,
)
from engine.core.models import Tier

CHALLENGE = (
    b"<html><head><title>g2.com</title></head><body>"
    b'<script src="https://geo.captcha-delivery.com/captcha/?initialCid=x"></script>'
    b"</body></html>"
)
REAL_PAGE = b"<html><body>" + b"genuine review content " * 400 + b"</body></html>"
DD_HEADERS = {"x-datadome": "protected", "content-type": "text/html"}


@dataclass
class FlakyFetcher:
    """Challenges for the first `fail_times` calls, then serves the page."""

    name: str
    fail_times: int
    calls: int = 0
    seen: list[int] = field(default_factory=list)

    async def fetch(self, req: FetchRequest) -> FetchResult:
        self.calls += 1
        self.seen.append(self.calls)
        challenged = self.calls <= self.fail_times
        body = CHALLENGE if challenged else REAL_PAGE
        return FetchResult(
            url=req.url,
            status_code=403 if challenged else 200,
            headers=dict(DD_HEADERS),
            body=body,
            content_type="text/html",
            tier=self.name,
            latency_ms=100,
            bytes_transferred=len(body),
        )

    async def healthcheck(self) -> bool:
        return True


def _request() -> FetchRequest:
    return FetchRequest(url="https://www.g2.com/products/slack/reviews", timeout_ms=300_000)


def _profile() -> DomainProfile:
    return DomainProfile(domain="g2.com", min_working_tier=Tier.STEALTH_HARD)


async def test_a_challenge_at_the_top_rung_is_retried_on_a_new_session() -> None:
    fetcher = FlakyFetcher(name="stealth_hard", fail_times=1)
    controller = EscalationController({Tier.STEALTH_HARD: fetcher})

    outcome = await controller.fetch(_request(), _profile())

    assert outcome.succeeded, "the second attempt served the real page"
    assert fetcher.calls == 2


async def test_it_gives_up_after_the_bounded_number_of_retries() -> None:
    """A site that is genuinely refusing must not be asked for ever."""
    fetcher = FlakyFetcher(name="stealth_hard", fail_times=99)
    controller = EscalationController({Tier.STEALTH_HARD: fetcher})

    outcome = await controller.fetch(_request(), _profile())

    assert not outcome.succeeded
    assert fetcher.calls == CHALLENGE_RETRIES + 1, "one attempt plus the retries"


async def test_a_cheap_rung_is_not_retried_on_a_challenge() -> None:
    """tier 0 never passes a device check; retrying it is pure latency."""
    http = FlakyFetcher(name="http", fail_times=99)
    controller = EscalationController({Tier.HTTP: http})

    await controller.fetch(
        FetchRequest(url="https://www.g2.com/x", timeout_ms=300_000),
        DomainProfile(domain="g2.com", min_working_tier=Tier.HTTP),
    )

    assert http.calls == 1, "cheap rungs escalate, they do not retry"


async def test_the_retry_still_respects_the_deadline() -> None:
    """A retry must never push a request past the caller's timeout."""
    fetcher = FlakyFetcher(name="stealth_hard", fail_times=99)
    controller = EscalationController({Tier.STEALTH_HARD: fetcher})

    outcome = await controller.fetch(
        FetchRequest(url="https://www.g2.com/x", timeout_ms=1),
        _profile(),
    )

    assert not outcome.succeeded
    assert fetcher.calls == 1, "no budget for a second attempt"
