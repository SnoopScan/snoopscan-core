"""Which requests are narrowed to premium providers: only hard work, on evidence.

The grade used to default to premium: a domain was budget work only after
several clean successes at the plain rungs, so every first visit and every
browser rung paid 2-4x per GB. Measured over the week to 25 Sep 2026 the
budget pools were no worse at the plain rungs and close at the browser ones,
so the default is now no grade at all — the cheapest healthy provider — and a
budget pool that fails on a site is passed over there on its record.
"""

from __future__ import annotations

from dataclasses import replace

from engine.core.fetch.escalation import DomainProfile
from engine.core.models import ScrapeOptions, Tier
from engine.core.scrape_service import proxy_grade

EASY = DomainProfile("docs.example.com", success_count=12, min_working_tier=Tier.HTTP)


def test_ordinary_work_is_not_narrowed_so_the_cheapest_provider_takes_it() -> None:
    assert proxy_grade(EASY, ScrapeOptions()) is None
    assert proxy_grade(DomainProfile("new.example.com"), ScrapeOptions()) is None
    # A browser rung or an old block is not a firewall: the provider record
    # decides those, provider by provider.
    assert proxy_grade(replace(EASY, min_working_tier=Tier.BROWSER), ScrapeOptions()) is None
    assert proxy_grade(replace(EASY, block_count=1), ScrapeOptions()) is None


def test_a_known_firewall_makes_it_premium() -> None:
    assert proxy_grade(replace(EASY, detected_waf="cloudflare"), ScrapeOptions()) == "premium"


def test_clicking_or_phone_requests_are_premium() -> None:
    assert proxy_grade(EASY, ScrapeOptions(mobile=True)) == "premium"
    assert (
        proxy_grade(EASY, ScrapeOptions(actions=[{"type": "click", "selector": "#more"}]))
        == "premium"
    )
