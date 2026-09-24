"""Crawl frontier: policy filtering, honeypot avoidance, sitemap parsing."""

from __future__ import annotations

import pytest

from engine.core.frontier.discovery import (
    CrawlPolicy,
    SkipReason,
    default_sitemap_urls,
    extract_links,
    is_honeypot_anchor,
    parse_sitemap,
    sitemap_urls_from_robots,
)


def policy(**kwargs: object) -> CrawlPolicy:
    defaults: dict[str, object] = {"root_url": "https://example.com/blog/"}
    defaults.update(kwargs)
    return CrawlPolicy(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Policy filtering
# --------------------------------------------------------------------------


def test_same_host_url_passes() -> None:
    assert policy().evaluate("https://example.com/blog/post-1", 1) is None


def test_external_host_is_skipped_by_default() -> None:
    """Turning allowExternalLinks on with a high limit is how you accidentally
    crawl the internet."""
    assert policy().evaluate("https://other.com/page", 1) == SkipReason.EXTERNAL_HOST


def test_external_host_allowed_when_requested() -> None:
    p = policy(allow_external_links=True)
    assert p.evaluate("https://other.com/page", 1) is None


def test_backward_link_skipped_unless_allowed() -> None:
    assert policy().evaluate("https://example.com/about", 1) == SkipReason.BACKWARD
    assert policy(allow_backward_links=True).evaluate("https://example.com/about", 1) is None


def test_depth_limit_enforced() -> None:
    p = policy(max_depth=2)
    assert p.evaluate("https://example.com/blog/a", 2) is None
    assert p.evaluate("https://example.com/blog/a", 3) == SkipReason.DEPTH


def test_exclude_wins_over_include() -> None:
    p = policy(
        include_paths=(r"^/blog/.*",),
        exclude_paths=(r"^/blog/draft.*",),
        allow_backward_links=True,
    )
    assert p.evaluate("https://example.com/blog/live", 1) is None
    assert p.evaluate("https://example.com/blog/draft-1", 1) == SkipReason.EXCLUDE_PATH


def test_include_paths_reject_non_matching() -> None:
    p = policy(include_paths=(r"^/blog/.*",), allow_backward_links=True)
    assert p.evaluate("https://example.com/shop/item", 1) == SkipReason.INCLUDE_PATH


def test_paths_match_against_path_plus_query() -> None:
    p = policy(include_paths=(r"\?page=\d+",), allow_backward_links=True)
    assert p.evaluate("https://example.com/list?page=2", 1) is None
    assert p.evaluate("https://example.com/list", 1) == SkipReason.INCLUDE_PATH


def test_binary_extensions_skipped() -> None:
    p = policy(allow_backward_links=True)
    assert p.evaluate("https://example.com/file.zip", 1) == SkipReason.EXTENSION
    assert p.evaluate("https://example.com/video.mp4", 1) == SkipReason.EXTENSION
    assert p.evaluate("https://example.com/article", 1) is None


def test_non_http_scheme_skipped() -> None:
    assert policy().evaluate("ftp://example.com/file", 1) == SkipReason.SCHEME


# --------------------------------------------------------------------------
# Honeypot avoidance — 05-block-detection.md section 6
# --------------------------------------------------------------------------


HONEYPOT_PAGE = """
<html><body>
  <a href="/real-one">Real link</a>
  <a href="/trap-1" style="display:none">hidden</a>
  <a href="/trap-2" style="visibility: hidden">hidden</a>
  <a href="/trap-3" class="sr-only">screen reader only</a>
  <a href="/trap-4" style="position:absolute; left:-9999px">offscreen</a>
  <a href="/real-two" rel="nofollow">Legitimate nofollow</a>
</body></html>
"""


def test_hidden_links_are_marked_as_honeypots() -> None:
    """Cloudflare's AI Labyrinth embeds hidden links to generated pages;
    following one identifies us as a bot and downgrades the whole session."""
    links = extract_links(
        HONEYPOT_PAGE, "https://example.com/", policy(allow_backward_links=True), depth=0
    )
    by_path = {link.url.rsplit("/", 1)[-1]: link for link in links}

    for trap in ("trap-1", "trap-2", "trap-3", "trap-4"):
        assert by_path[trap].skip_reason == SkipReason.HONEYPOT, f"{trap} not caught"


def test_nofollow_alone_is_not_a_honeypot() -> None:
    """nofollow is common on perfectly legitimate links; it only counts in
    combination with hidden styling."""
    links = extract_links(
        HONEYPOT_PAGE, "https://example.com/", policy(allow_backward_links=True), depth=0
    )
    by_path = {link.url.rsplit("/", 1)[-1]: link for link in links}
    assert by_path["real-two"].skip_reason is None
    assert by_path["real-one"].skip_reason is None


def test_skipped_links_are_recorded_not_dropped() -> None:
    """Silent drops make crawl behaviour impossible to debug."""
    links = extract_links(
        HONEYPOT_PAGE, "https://example.com/", policy(allow_backward_links=True), depth=0
    )
    assert len(links) == 6, "every link must be recorded, with or without a reason"
    assert all(link.skip_reason is None or isinstance(link.skip_reason, str) for link in links)


def test_zero_size_anchor_is_a_honeypot() -> None:
    from selectolax.parser import HTMLParser

    node = HTMLParser('<a href="/x" width="1" height="1">.</a>').css_first("a")
    assert is_honeypot_anchor(node)


# --------------------------------------------------------------------------
# Link extraction
# --------------------------------------------------------------------------


def test_relative_links_resolved_absolutely() -> None:
    html = '<html><body><a href="/blog/a">A</a><a href="b">B</a></body></html>'
    links = extract_links(html, "https://example.com/blog/", policy(), depth=0)
    urls = {link.url for link in links}
    assert "https://example.com/blog/a" in urls
    assert "https://example.com/blog/b" in urls


def test_fragment_and_mailto_links_ignored() -> None:
    html = (
        '<html><body><a href="#top">Top</a><a href="mailto:a@example.com">Mail</a>'
        '<a href="javascript:void(0)">JS</a><a href="/real">Real</a></body></html>'
    )
    links = extract_links(html, "https://example.com/blog/", policy(), depth=0)
    assert len(links) == 1
    assert links[0].url.endswith("/real")


def test_duplicate_links_collapse_once_normalised() -> None:
    html = (
        '<html><body><a href="/blog/a">1</a><a href="/blog/a/">2</a>'
        '<a href="/blog/a?utm_source=x">3</a></body></html>'
    )
    links = extract_links(html, "https://example.com/blog/", policy(), depth=0)
    assert len(links) == 1


def test_discovered_links_carry_depth_and_parent() -> None:
    html = '<html><body><a href="/blog/a">A</a></body></html>'
    links = extract_links(html, "https://example.com/blog/", policy(), depth=2)
    assert links[0].depth == 3
    assert links[0].parent_url == "https://example.com/blog/"
    assert links[0].discovered_via == "crawl"


# --------------------------------------------------------------------------
# Sitemaps
# --------------------------------------------------------------------------


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/b</loc></url>
</urlset>"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-1.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap-2.xml</loc></sitemap>
</sitemapindex>"""


def test_sitemap_urls_parsed() -> None:
    pages, nested = parse_sitemap(SITEMAP)
    assert pages == ["https://example.com/a", "https://example.com/b"]
    assert nested == []


def test_sitemap_index_returns_nested_sitemaps() -> None:
    pages, nested = parse_sitemap(SITEMAP_INDEX)
    assert pages == []
    assert len(nested) == 2


def test_malformed_sitemap_yields_nothing_rather_than_raising() -> None:
    """One bad sitemap must not fail the crawl that referenced it."""
    assert parse_sitemap("<not xml") == ([], [])
    assert parse_sitemap("") == ([], [])


def test_xml_entity_expansion_is_refused() -> None:
    """Sitemaps are attacker-controlled on every site we crawl. A billion-laughs
    payload must be refused, not expanded — stdlib ElementTree would expand it
    and take the worker down."""
    bomb = """<?xml version="1.0"?>
    <!DOCTYPE lolz [
      <!ENTITY lol "lol">
      <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
      <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
    ]>
    <urlset><url><loc>&lol3;</loc></url></urlset>"""
    assert parse_sitemap(bomb) == ([], [])


def test_sitemap_directives_read_from_robots() -> None:
    robots = """
    User-agent: *
    Disallow: /admin
    Sitemap: https://example.com/sitemap.xml
    Sitemap: https://example.com/news-sitemap.xml
    """
    assert sitemap_urls_from_robots(robots) == [
        "https://example.com/sitemap.xml",
        "https://example.com/news-sitemap.xml",
    ]


def test_default_sitemap_locations() -> None:
    urls = default_sitemap_urls("https://example.com/blog/post")
    assert "https://example.com/sitemap.xml" in urls


@pytest.mark.parametrize(
    ("root", "candidate", "expected_skip"),
    [
        ("https://example.com/", "https://blog.example.com/x", SkipReason.EXTERNAL_HOST),
        ("https://example.com/", "https://example.com/x", None),
    ],
)
def test_subdomains_excluded_by_default(
    root: str, candidate: str, expected_skip: str | None
) -> None:
    p = CrawlPolicy(root_url=root, allow_backward_links=True)
    assert p.evaluate(candidate, 1) == expected_skip
