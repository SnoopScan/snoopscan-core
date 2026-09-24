"""A plain 403 from a domain already known to be DataDome is still a coin toss.

The challenge retry was keyed on the RESPONSE: a captcha-delivery loader, a
challenge title. g2.com, measured live 18 Sep 2026 once stealth_hard could
finally run, answers the deep rungs with a bare 403 and no challenge markup at
all — so the verdict was `status_403`, the retry never fired, and the request
gave up after one IP on the rung that clears g2's review pages about one time
in five.

The DOMAIN already told us what the response did not: its profile says
DataDome, and DataDome decides per exit IP. So on a known-DataDome domain, at
the rungs where a pass is possible, a bare 403 gets the same fresh-IP retry a
recognised challenge does.

Kept narrow on purpose. An unknown domain's 403 is not retried — that could be
a genuine refusal, and asking three times would only triple the cost of
hearing no.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.core.fetch.base import FetchRequest, FetchResult
from engine.core.fetch.escalation import (
    CHALLENGE_RETRIES,
    DomainProfile,
    EscalationController,
)
from engine.core.models import Tier

# What g2 actually returned at stealth_hard: a 403, no x-datadome header, and
# no captcha-delivery loader — nothing in the response names the vendor.
BARE_403 = b"<html><head><title>403 Forbidden</title></head><body>Forbidden</body></html>"
REAL_PAGE = b"<html><body>" + b"genuine review content " * 400 + b"</body></html>"


@dataclass
class Bare403Fetcher:
    name: str
    fail_times: int
    calls: int = 0

    async def fetch(self, req: FetchRequest) -> FetchResult:
        self.calls += 1
        refused = self.calls <= self.fail_times
        body = BARE_403 if refused else REAL_PAGE
        return FetchResult(
            url=req.url,
            status_code=403 if refused else 200,
            headers={"content-type": "text/html"},
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


async def test_a_bare_403_on_a_known_datadome_domain_is_retried_on_a_new_ip() -> None:
    fetcher = Bare403Fetcher(name="stealth_hard", fail_times=1)
    controller = EscalationController({Tier.STEALTH_HARD: fetcher})
    profile = DomainProfile(
        domain="g2.com", min_working_tier=Tier.STEALTH_HARD, detected_waf="datadome"
    )

    outcome = await controller.fetch(_request(), profile)

    assert outcome.succeeded, "the second IP served the real page"
    assert fetcher.calls == 2


async def test_the_retry_is_bounded_like_any_other_challenge() -> None:
    fetcher = Bare403Fetcher(name="stealth_hard", fail_times=99)
    controller = EscalationController({Tier.STEALTH_HARD: fetcher})
    profile = DomainProfile(
        domain="g2.com", min_working_tier=Tier.STEALTH_HARD, detected_waf="datadome"
    )

    outcome = await controller.fetch(_request(), profile)

    assert not outcome.succeeded
    assert fetcher.calls == CHALLENGE_RETRIES + 1


async def test_an_unknown_domains_403_is_not_retried() -> None:
    """No WAF on record: the 403 may be a real refusal. Hear it once."""
    fetcher = Bare403Fetcher(name="stealth_hard", fail_times=99)
    controller = EscalationController({Tier.STEALTH_HARD: fetcher})
    profile = DomainProfile(domain="example.org", min_working_tier=Tier.STEALTH_HARD)

    await controller.fetch(_request(), profile)

    assert fetcher.calls == 1


async def test_a_cheap_rung_still_escalates_rather_than_retries() -> None:
    """Even on a DataDome domain, tier 0 never passes; retrying it is latency."""
    http = Bare403Fetcher(name="http", fail_times=99)
    controller = EscalationController({Tier.HTTP: http})
    profile = DomainProfile(domain="g2.com", min_working_tier=Tier.HTTP, detected_waf="datadome")

    await controller.fetch(_request(), profile)

    assert http.calls == 1
