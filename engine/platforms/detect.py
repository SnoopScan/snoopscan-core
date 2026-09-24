"""Which platform built this site? From the homepage bytes and headers.

Every needle here was measured on a live site (fixtures/platforms/homepages.json).
Order matters where platforms nest: WooCommerce is WordPress, so it is checked
first; a Substack page contains the word "framer" in a script, so Framer is
detected by its generator meta and nothing looser.
"""

from __future__ import annotations

import re
from enum import StrEnum


class Platform(StrEnum):
    SHOPIFY = "shopify"
    WOOCOMMERCE = "woocommerce"
    WORDPRESS = "wordpress"
    SQUARESPACE = "squarespace"
    SUBSTACK = "substack"
    DISCOURSE = "discourse"
    GHOST = "ghost"
    DRUPAL = "drupal"
    MEDIAWIKI = "mediawiki"
    WEBFLOW = "webflow"
    FRAMER = "framer"
    WIX = "wix"
    MAGENTO = "magento"
    AMAZON = "amazon"
    MASTODON = "mastodon"
    BLUESKY = "bluesky"


_GENERATOR_RE = re.compile(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', re.I)


def _generator(html: str) -> str:
    m = _GENERATOR_RE.search(html)
    return (m.group(1) if m else "").lower()


def detect_platform(html: str, headers: dict[str, str] | None = None) -> Platform | None:
    """The homepage is enough. Returns None rather than guessing."""
    h = html[:400_000].lower()
    hd = {k.lower(): (v or "").lower() for k, v in (headers or {}).items()}
    link = hd.get("link", "")
    gen = _generator(html)

    if (
        "x-shopid" in hd
        or "x-shopify-stage" in hd
        or "cdn.shopify.com" in h
        or "shopify.theme" in h
    ):
        return Platform.SHOPIFY
    is_wp = (
        "api.w.org" in link
        or "/wp-content/" in h
        or "/wp-json/" in h
        or gen.startswith("wordpress")
    )
    if is_wp and ("woocommerce" in h or "wc-ajax" in h):
        return Platform.WOOCOMMERCE
    if is_wp:
        return Platform.WORDPRESS
    if (
        "static1.squarespace.com" in h
        or ("squarespace.com" in h and "squarespace-cdn" in h)
        or gen.startswith("squarespace")
    ):
        return Platform.SQUARESPACE
    if "substackcdn.com" in h or ("substack.com" in h and "substack-" in h):
        return Platform.SUBSTACK
    if (
        "discourse" in gen
        or 'id="data-discourse-setup"' in h
        or "discourse-cdn" in h
        or "/assets/discourse" in h
    ):
        return Platform.DISCOURSE
    if gen.startswith("ghost") or "ghost.io" in h or "/ghost/api/" in h:
        return Platform.GHOST
    if "drupal" in hd.get("x-generator", "") or gen.startswith("drupal") or "x-drupal-cache" in hd:
        return Platform.DRUPAL
    if gen.startswith("mediawiki") or "mw.config.set" in h or "/load.php?" in h:
        return Platform.MEDIAWIKI
    if "website-files.com" in h or gen.startswith("webflow") or "data-wf-page" in h:
        return Platform.WEBFLOW
    if gen.startswith("framer") or "framerusercontent.com" in h:
        return Platform.FRAMER
    if "x-wix-request-id" in hd or "static.wixstatic.com" in h or "wix.com" in gen:
        return Platform.WIX
    # Mastodon before the store checks: its pages carry `image/` too, and the
    # old bare "mage/" Magento needle matched that — a profile read as a Magento
    # store (6 Sep 2026). Its own markers, none of which a shop has.
    if "joinmastodon" in h or 'id="mastodon"' in h or gen.startswith("mastodon"):
        return Platform.MASTODON
    # Magento 2 by a REAL marker only. `mage/` is gone: it is a substring of
    # "image/" and matched everything, which is the whole reason Magento
    # detection ever "worked". Recall is lower now, so `/v1/products` also probes
    # the storefront GraphQL endpoint (service.products) — the thing that
    # actually defines a Magento store — when passive detection finds nothing.
    if (
        "data-mage-init" in h
        or "/static/version" in h
        or "/static/frontend/" in h
        or "magento_" in h
        or "mage-cache-storage" in h
        or gen.startswith("magento")
    ):
        return Platform.MAGENTO
    return None


_AMAZON_HOST = re.compile(
    r"(^|\.)amazon\.(com|co\.uk|de|fr|it|es|ca|com\.au|co\.jp|in|nl|se|pl|com\.br|com\.mx|ae|sg)$"
)


def detect_platform_for_url(
    url: str, html: str, headers: dict[str, str] | None = None
) -> Platform | None:
    """detect_platform, plus the platforms only the hostname reveals."""
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower()
    if _AMAZON_HOST.search(host):
        return Platform.AMAZON
    if host == "bsky.app" or host.endswith((".bsky.app", ".bsky.social")):
        return Platform.BLUESKY
    return detect_platform(html, headers)
