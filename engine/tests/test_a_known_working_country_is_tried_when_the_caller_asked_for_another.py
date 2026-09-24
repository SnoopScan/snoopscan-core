"""A domain's known working country is tried when the caller asked elsewhere.

indeed.com, 22 Sep 2026: the domain was on record as answering from GB. A
caller asked for the US — the caller's country wins on the first attempt — and
all five rungs were refused from US exits. The retry from another country then
returned nothing, because it skipped whenever a working country was on record
("already known; it was used on the first attempt"). It had not been used: the
caller's choice had replaced it. The one country known to work was never tried.
"""

from __future__ import annotations

from engine.core.fetch.escalation import DomainProfile
from engine.core.scrape_service import countries_to_retry


def test_the_known_country_is_tried_when_the_caller_asked_for_another() -> None:
    profile = DomainProfile("indeed.com", working_country="gb")
    assert countries_to_retry(profile, "us", 2) == ["gb", "gb"]
    assert countries_to_retry(profile, "US", 2) == ["gb", "gb"]


def test_the_known_country_is_retried_from_fresh_exits_even_when_it_was_asked_for() -> None:
    """The first attempt may have gone direct, and one refused exit is not the
    country refusing: indeed.com answered the same provider and country 2 in 4.
    """
    profile = DomainProfile("indeed.com", working_country="gb")
    assert countries_to_retry(profile, None, 2) == ["gb", "gb"]
    assert countries_to_retry(profile, "gb", 2) == ["gb", "gb"]
    assert countries_to_retry(profile, "gb", 1) == ["gb"]


def test_an_unknown_domain_still_tours_the_fallbacks_it_has_not_tried() -> None:
    profile = DomainProfile("example.com", country_attempts=("gb",))
    assert countries_to_retry(profile, "us", 2) == ["de"]
    assert countries_to_retry(DomainProfile("example.com"), None, 2) == ["us", "gb"]
