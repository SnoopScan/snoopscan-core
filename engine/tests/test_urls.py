"""URL normalisation and registrable-domain extraction."""

from __future__ import annotations

import pytest

from engine.core.urls import (
    has_skipped_extension,
    is_subdomain_of,
    normalize_url,
    normalized_hash,
    registrable_domain,
    url_hash,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # lowercase scheme and host
        ("HTTPS://Example.COM/Path", "https://example.com/Path"),
        # strip default port
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        # keep a non-default port
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        # remove fragment
        ("https://example.com/a#section", "https://example.com/a"),
        # sort query params
        ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
        # drop tracking params
        ("https://example.com/a?utm_source=x&id=7", "https://example.com/a?id=7"),
        ("https://example.com/a?fbclid=abc", "https://example.com/a"),
        ("https://example.com/a?gclid=1&msclkid=2&keep=3", "https://example.com/a?keep=3"),
        # trailing slash stripped unless root
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/", "https://example.com/"),
    ],
)
def test_normalisation(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_drop_query_entirely() -> None:
    assert normalize_url("https://example.com/a?x=1", drop_query=True) == "https://example.com/a"


def test_urls_differing_only_by_tracking_dedup_together() -> None:
    a = normalized_hash("https://example.com/post?utm_source=twitter")
    b = normalized_hash("https://example.com/post/")
    assert a == b


def test_url_hash_is_identity_not_dedup() -> None:
    """url_hash distinguishes what normalized_hash deliberately collapses."""
    a = url_hash("https://example.com/post?utm_source=twitter")
    b = url_hash("https://example.com/post")
    assert a != b


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The case that naive dot-splitting gets wrong.
        ("https://shop.example.co.uk/x", "example.co.uk"),
        ("example.co.uk", "example.co.uk"),
        ("https://www.example.com", "example.com"),
        ("https://deep.sub.example.com/a", "example.com"),
        ("https://example.github.io", "example.github.io"),
        ("https://example.com.au/x", "example.com.au"),
    ],
)
def test_registrable_domain(value: str, expected: str) -> None:
    assert registrable_domain(value) == expected


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        # Reserved TLDs and any gTLD newer than the bundled suffix snapshot.
        ("crawl.example.invalid", "example.invalid"),
        ("a.b.example.test", "example.test"),
        ("shop.example.somebrandnewtld", "example.somebrandnewtld"),
    ],
)
def test_unknown_suffix_still_yields_a_distinct_domain(host: str, expected: str) -> None:
    """An unknown TLD must not collapse every host under it onto one key.

    It did once: `crawl.example.invalid` and `elsewhere.example.invalid` both
    resolved to `invalid`, which would pool politeness budgets and tier memory
    across completely unrelated sites.
    """
    assert registrable_domain(host) == expected


def test_two_hosts_under_an_unknown_tld_stay_separate() -> None:
    assert registrable_domain("crawl.example.invalid") != registrable_domain("other.site.invalid")


def test_skip_extensions() -> None:
    assert has_skipped_extension("https://example.com/file.zip")
    assert has_skipped_extension("https://example.com/a/video.mp4")
    assert not has_skipped_extension("https://example.com/article")
    assert not has_skipped_extension("https://example.com/page.html")


def test_subdomain_matching() -> None:
    assert is_subdomain_of("blog.example.com", "example.com")
    assert is_subdomain_of("example.com", "example.com")
    # Must not match a domain that merely ends with the same string.
    assert not is_subdomain_of("notexample.com", "example.com")
