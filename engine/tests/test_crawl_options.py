"""Crawl option derivation.

The formats a crawl requests are not purely cosmetic: link discovery and
honeypot screening both depend on them, and getting that wrong is silent.
"""

from __future__ import annotations

from engine.core.frontier.crawler import _scrape_options_for
from engine.core.models import CrawlRequest, ScrapeOptions


def request(**kwargs: object) -> CrawlRequest:
    payload: dict[str, object] = {"url": "https://example.com/"}
    payload.update(kwargs)
    return CrawlRequest(**payload)  # type: ignore[arg-type]


def test_crawl_always_requests_html_for_honeypot_screening() -> None:
    """Regression: with markdown-only formats, link discovery fell back to a
    bare URL list, which carries no anchor attributes — so hidden-link
    detection silently did nothing and the crawler followed honeypot links.
    """
    options = _scrape_options_for(request(scrapeOptions=ScrapeOptions(formats=["markdown"])))
    assert options._has_format("html"), "honeypot screening needs the source anchors"


def test_crawl_always_requests_links() -> None:
    options = _scrape_options_for(request(scrapeOptions=ScrapeOptions(formats=["markdown"])))
    assert options._has_format("links")


def test_caller_formats_are_preserved() -> None:
    options = _scrape_options_for(
        request(scrapeOptions=ScrapeOptions(formats=["markdown", "rawHtml"]))
    )
    assert options._has_format("markdown")
    assert options._has_format("rawHtml")


def test_no_duplicate_formats_when_already_requested() -> None:
    options = _scrape_options_for(
        request(scrapeOptions=ScrapeOptions(formats=["markdown", "html", "links"]))
    )
    simple = [f for f in options.formats if isinstance(f, str)]
    assert len(simple) == len(set(simple))


def test_respect_robots_flows_from_the_crawl_request() -> None:
    options = _scrape_options_for(request(respectRobots=False))
    assert options.respectRobots is False

    options = _scrape_options_for(request(respectRobots=True))
    assert options.respectRobots is True
