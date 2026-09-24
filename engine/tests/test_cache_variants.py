"""The page cache is shared between every customer, so its key must describe
the page — not just the URL that produced it.

Measured live on 4 September 2026: two scrapes of ipinfo.io, the first through
a US exit, the second explicitly asking for GB with a ten-minute maxAge. The GB
caller was served the US page and told it was a cache hit. Same failure shape as
a search parameter nobody honoured — you ask for one thing, are given another,
and the response looks perfect.

The second half is worse and was never exercised: the key ignored request
headers too, so a page fetched with one caller's session cookie would answer
another caller's request for the same URL.
"""

from __future__ import annotations

from engine.core.models import Location, ProxyMode, ScrapeOptions
from engine.core.scrape_service import ScrapeService
from engine.core.urls import normalized_hash, variant_hash

URL = "https://ipinfo.io/json"


def key(**kw: object) -> bytes:
    return ScrapeService._cache_variant(URL, ScrapeOptions(**kw))[0]  # type: ignore[arg-type]


def shareable(**kw: object) -> bool:
    return ScrapeService._cache_variant(URL, ScrapeOptions(**kw))[1]  # type: ignore[arg-type]


def test_two_countries_are_two_documents() -> None:
    assert key(location=Location(country="US")) != key(location=Location(country="GB"))


def test_a_phone_and_a_desktop_are_two_documents() -> None:
    assert key(mobile=True) != key(mobile=False)


def test_direct_and_proxied_are_two_documents() -> None:
    """Same country, different route out, frequently a different page."""
    assert key(proxy=ProxyMode.NONE) != key(proxy=ProxyMode.RESIDENTIAL)


def test_the_same_request_is_the_same_key() -> None:
    assert key(location=Location(country="gb")) == key(location=Location(country="GB"))


def test_the_variant_key_is_not_the_dedup_key() -> None:
    """Crawl dedup still asks 'is this the same page' — two routes to one URL
    are one page. The cache asks a different question and needs its own key.
    """
    assert key(location=Location(country="US")) != normalized_hash(URL)
    assert variant_hash(URL) != normalized_hash(URL)


def test_a_fetch_carrying_caller_headers_is_never_shared() -> None:
    """The one that would have hurt most: a session cookie makes the response
    personal, and this cache serves everybody.
    """
    assert shareable() is True
    assert shareable(headers={"Cookie": "session=abc123"}) is False
    assert shareable(headers={"Authorization": "Bearer tok"}) is False
