"""Platform detection and feed parsing, against real captures (6 Sep 2026).

fixtures/platforms/homepages.json holds the first 60KB and the relevant headers
of twelve live homepages; the feeds are real RSS and Atom documents trimmed to
three entries. Nothing here was written from a spec — every needle was measured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.platforms.detect import Platform, detect_platform
from engine.platforms.feeds import declared_feeds, looks_like_feed, parse_feed
from engine.tests.fixtures.platforms import load

FIX = Path(__file__).parent / "fixtures" / "platforms"
HOMES = json.loads(load("homepages.json"))


@pytest.mark.parametrize(
    ("site", "expected"),
    [
        ("shopify", Platform.SHOPIFY),
        ("woocommerce", Platform.WOOCOMMERCE),
        ("wordpress", Platform.WORDPRESS),
        ("squarespace", Platform.SQUARESPACE),
        ("substack", Platform.SUBSTACK),
        ("discourse", Platform.DISCOURSE),
        ("ghost", Platform.GHOST),
        ("drupal", Platform.DRUPAL),
        ("webflow", Platform.WEBFLOW),
        ("framer", Platform.FRAMER),
        ("mediawiki", Platform.MEDIAWIKI),
        ("mastodon", Platform.MASTODON),
    ],
)
def test_each_live_homepage_is_recognised(site: str, expected: Platform) -> None:
    h = HOMES[site]
    assert detect_platform(h["head"], h["headers"]) == expected, site


def test_a_plain_page_is_not_guessed_at() -> None:
    h = HOMES["plain"]
    assert detect_platform(h["head"], h["headers"]) is None


def test_woocommerce_wins_over_wordpress_which_it_is_built_on() -> None:
    html = '<link rel="https://api.w.org/" href="/wp-json/"><body class="woocommerce-page">'
    assert detect_platform(html, {}) == Platform.WOOCOMMERCE
    wp_only = '<link rel="https://api.w.org/" href="/wp-json/">'
    assert detect_platform(wp_only, {}) == Platform.WORDPRESS


def test_a_bluesky_url_is_detected_by_host_not_body() -> None:
    from engine.platforms.detect import detect_platform_for_url

    assert detect_platform_for_url("https://bsky.app/profile/bsky.app", "", {}) == Platform.BLUESKY
    assert detect_platform_for_url("https://a.bsky.social/profile/x", "", {}) == Platform.BLUESKY


def test_image_slash_no_longer_reads_as_magento() -> None:
    """The bug: `mage/` matched `image/`, so any page with an image was Magento —
    a Mastodon profile among them. A real image reference is not a storefront."""
    assert detect_platform('<img src="/assets/image/logo.png">', {}) is None


def test_the_word_framer_in_a_script_is_not_framer() -> None:
    """Substack pages contain 'framer' in a bundle; only the generator counts."""
    html = '<script>window.framer = 1</script><img src="https://substackcdn.com/x">'
    assert detect_platform(html, {}) == Platform.SUBSTACK


# --------------------------------------------------------------------------
# feeds
# --------------------------------------------------------------------------


def test_rss_from_a_ghost_blog_parses_to_posts() -> None:
    posts = parse_feed((FIX / "ghost_rss.xml").read_text(), platform="ghost")
    assert posts, "three items were captured"
    assert all(p.title and p.url.startswith("http") for p in posts)
    assert posts[0].published_at


def test_rss_from_wordpress_parses_to_posts() -> None:
    posts = parse_feed((FIX / "wordpress_feed.xml").read_text(), platform="wordpress")
    assert posts and posts[0].url.startswith("https://barefootbuttons.example/")


def test_atom_parses_to_posts() -> None:
    posts = parse_feed((FIX / "atom.xml").read_text())
    assert posts and posts[0].url.startswith("https://xkcd.com/")


def test_a_broken_feed_is_an_empty_list_not_an_exception() -> None:
    assert parse_feed("<rss><channel><item><title>oops") == []
    assert parse_feed("not xml at all") == []


def test_feed_discovery_reads_the_alternate_link() -> None:
    html = (
        '<link rel="alternate" type="application/rss+xml" href="/rss/">'
        '<link rel="alternate" type="application/atom+xml" href="https://x.test/atom">'
    )
    assert declared_feeds(html, "https://x.test/") == ["https://x.test/rss/", "https://x.test/atom"]


def test_looks_like_feed_rejects_html_and_accepts_both_dialects() -> None:
    assert looks_like_feed(b'<?xml version="1.0"?><rss version="2.0">')
    assert looks_like_feed(b'<feed xmlns="http://www.w3.org/2005/Atom">')
    assert not looks_like_feed(b"<!doctype html><html>")
