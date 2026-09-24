"""Whose spend populated a cached row decides what re-reading it costs.

The cache is shared across every customer and a hit priced at zero, so the
99th account to want a URL paid nothing for work the 1st account paid for,
and the more popular a page was the less it earned. Measured 8 Sep 2026:
22,162 pages sat inside the live 48-hour window, worth 38,441 credits at our
own table, billing nothing however many accounts read them.

Firecrawl runs the same 48-hour default and charges for every cached page.
SerpApi's free cache is a ONE-HOUR window — a retry convenience, not a corpus
to harvest.
"""

from __future__ import annotations

from engine.core.credits import credits_for
from engine.core.models import ScrapeOptions
from engine.core.scrape_service import _data_from_page_row

ROW = {
    "id": "page_1",
    "url": "https://ex.test/a",
    "markdown": "# A",
    "html": None,
    "raw_html": None,
    "links": None,
    "fetch_tier": "stealth_hard",
    "tiers_attempted": ["http", "stealth_hard"],
    "extraction_path": "heuristic",
    "fetched_by": "owner-A",
    "title": None,
    "description": None,
    "language": None,
    "author": None,
    "published_at": None,
    "page_type": "article",
    "word_count": 1,
    "extraction_confidence": 0.9,
    "status_code": 200,
    "content_type": "text/html",
    "source_url": "https://ex.test/a",
}
OPTIONS = ScrapeOptions(formats=["markdown"])


def price(row: dict, owner: str | None) -> int:
    data = _data_from_page_row(row, "https://ex.test/a", OPTIONS, owner_ref=owner)
    return credits_for(data.cost)


def test_your_own_row_is_free_to_re_read() -> None:
    assert price(ROW, "owner-A") == 0


def test_another_accounts_row_is_charged() -> None:
    """It is still a cache hit — instant, no fetch — but it is not work this
    customer paid for."""
    assert price(ROW, "owner-B") == 1


def test_an_unowned_row_charges_rather_than_gives_away() -> None:
    """Rows stored before attribution existed, and rows stored by a monitor
    with no owner. The safe direction: it costs at most one re-fetch to
    correct, and corrects itself as rows age out of the window."""
    assert price({**ROW, "fetched_by": None}, "owner-A") == 1


def test_an_anonymous_caller_is_charged_too() -> None:
    """No owner on the request cannot mean everything is free."""
    assert price(ROW, None) == 1


def test_a_row_missing_the_column_entirely_still_prices() -> None:
    """A row from a build that predates the migration must not raise."""
    row = {k: v for k, v in ROW.items() if k != "fetched_by"}

    assert price(row, "owner-A") == 1


def test_re_reading_never_costs_more_than_the_original_fetch() -> None:
    """stealth_hard is 5 fresh. If a cache hit ever exceeded that, callers
    would rationally set maxAge=0 and we would pay for every page twice."""
    from engine.core.models import Cost

    fresh = credits_for(Cost(tier="stealth_hard"))

    assert price(ROW, "owner-A") <= price(ROW, "owner-B") <= fresh
    assert fresh == 5


def test_the_cost_object_says_which_it_was() -> None:
    """Support has to be able to answer "why was I charged for a cache hit"."""
    own = _data_from_page_row(ROW, "https://ex.test/a", OPTIONS, owner_ref="owner-A").cost
    other = _data_from_page_row(ROW, "https://ex.test/a", OPTIONS, owner_ref="owner-B").cost

    assert own.cached and own.cache_own
    assert other.cached and not other.cache_own
    # Both must still carry what the page originally took to fetch.
    assert own.tier == other.tier == "stealth_hard"
