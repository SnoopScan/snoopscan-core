"""Which requests are budget work: only a domain that is cheap on the record.

A budget provider costs a fraction of a premium one per GB and, measured on a
defended job board, fails where premium ones get through. So the rule is
evidence-only: several clean successes at the plain rungs and not one block.
Anything unknown or ever-difficult is premium.
"""

from __future__ import annotations

from dataclasses import replace

from engine.core.fetch.escalation import DomainProfile
from engine.core.models import ScrapeOptions, Tier
from engine.core.scrape_service import proxy_grade

EASY = DomainProfile("docs.example.com", success_count=12, min_working_tier=Tier.HTTP)


def test_a_domain_proven_easy_is_budget_work() -> None:
    assert proxy_grade(EASY, ScrapeOptions()) == "budget"
    assert (
        proxy_grade(replace(EASY, min_working_tier=Tier.IMPERSONATE), ScrapeOptions()) == "budget"
    )


def test_a_domain_seen_for_the_first_time_is_premium() -> None:
    assert proxy_grade(DomainProfile("new.example.com"), ScrapeOptions()) == "premium"
    assert proxy_grade(replace(EASY, success_count=2), ScrapeOptions()) == "premium"


def test_one_block_anywhere_on_the_record_makes_it_premium() -> None:
    assert proxy_grade(replace(EASY, block_count=1), ScrapeOptions()) == "premium"


def test_a_known_firewall_or_a_browser_rung_makes_it_premium() -> None:
    assert proxy_grade(replace(EASY, detected_waf="cloudflare"), ScrapeOptions()) == "premium"
    assert proxy_grade(replace(EASY, min_working_tier=Tier.BROWSER), ScrapeOptions()) == "premium"


def test_clicking_or_phone_requests_are_premium() -> None:
    assert proxy_grade(EASY, ScrapeOptions(mobile=True)) == "premium"
    assert (
        proxy_grade(EASY, ScrapeOptions(actions=[{"type": "click", "selector": "#more"}]))
        == "premium"
    )
