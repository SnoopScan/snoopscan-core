"""A feed URL never climbs into the browser tiers.

A browser wraps XML in its own viewer, so no rung above impersonate can return
a feed — each one only costs more. Measured 17 Sep 2026 on a Reddit thread feed:
`auto` walked http -> impersonate -> browser -> stealth for 5 credits and zero
entries, while residential at tier 0 returned 32 entries for 2.
"""

from __future__ import annotations

import pytest

from engine.core.fetch.escalation import ladder_from
from engine.core.models import Tier
from engine.core.scrape_service import _is_feed_url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.reddit.com/r/webscraping/comments/12hh9gi/proxies_are_expensive/.rss",
        "https://www.reddit.com/r/webscraping/search.rss?q=proxy&restrict_sr=1",
        "https://example.com/blog/feed",
        "https://example.com/blog/feed/",
        "https://example.com/atom",
        "https://example.com/sitemap.xml",
        "https://example.com/index.php?format=rss",
        "https://example.com/?feed=atom",
    ],
)
def test_feeds_are_recognised(url: str) -> None:
    assert _is_feed_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.reddit.com/r/webscraping/comments/12hh9gi/proxies_are_expensive/",
        "https://example.com/feedback",
        "https://example.com/blog/rss-readers-compared",
        "https://example.com/pricing",
        "https://example.com/?q=rss",
    ],
)
def test_ordinary_pages_are_not_feeds(url: str) -> None:
    """A false positive would cap a real page at the HTTP tiers."""
    assert not _is_feed_url(url)


def test_the_feed_ceiling_keeps_the_ladder_on_http_tiers() -> None:
    ladder = ladder_from(Tier.HTTP, max_tier=Tier.IMPERSONATE)
    assert ladder == [Tier.HTTP, Tier.IMPERSONATE]
    assert Tier.BROWSER not in ladder
