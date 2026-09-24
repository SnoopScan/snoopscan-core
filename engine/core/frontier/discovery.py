"""URL discovery and filtering for the crawl frontier (07-orchestration.md s3).

Sitemap first — it is cheaper, faster and more complete than discovering the
same URLs by crawling, and it is also the polite option.

Skipped URLs are recorded with a reason rather than silently dropped. Silent
drops make crawl behaviour impossible to debug after the fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

# ParseError is the same class defusedxml re-raises for malformed input.
from xml.etree.ElementTree import ParseError  # noqa: N817

# defusedxml, not the stdlib parser. Sitemaps are attacker-controlled input on
# every site we crawl, and stdlib ElementTree is vulnerable to entity-expansion
# ("billion laughs") and quadratic-blowup attacks that would take a worker down.
from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException
from selectolax.parser import HTMLParser

from engine.core.urls import (
    has_skipped_extension,
    host_of,
    is_subdomain_of,
    normalize_url,
    normalized_hash,
    registrable_domain,
    url_hash,
)

# Namespaces used by sitemap XML.
_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Inline styles and class names that hide an element. A hidden link is a
# honeypot: Cloudflare's AI Labyrinth embeds hidden nofollow links to its
# generated pages, and following one identifies us as a bot.
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?!\.)"
    r"|(?:left|top)\s*:\s*-\d{3,}|font-size\s*:\s*0",
    re.IGNORECASE,
)
_HIDDEN_CLASSES = (
    "hidden",
    "hide",
    "sr-only",
    "visually-hidden",
    "screen-reader",
    "invisible",
    "offscreen",
    "off-screen",
)


class SkipReason:
    SCHEME = "scheme"
    EXTERNAL_HOST = "external_host"
    BACKWARD = "backward_link"
    DEPTH = "max_depth"
    INCLUDE_PATH = "include_path"
    EXCLUDE_PATH = "exclude_path"
    EXTENSION = "extension"
    HONEYPOT = "honeypot"
    ROBOTS = "robots_denied"


@dataclass
class CrawlPolicy:
    """The filtering rules for one crawl job."""

    root_url: str
    max_depth: int = 3
    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    allow_external_links: bool = False
    allow_backward_links: bool = False
    include_subdomains: bool = False
    ignore_query_parameters: bool = False

    def __post_init__(self) -> None:
        self._include = [re.compile(p) for p in self.include_paths]
        self._exclude = [re.compile(p) for p in self.exclude_paths]
        parts = urlsplit(self.root_url)
        self.root_host = (parts.hostname or "").lower()
        self.root_domain = registrable_domain(self.root_url)
        root_path = parts.path or "/"
        self.root_path = root_path if root_path.endswith("/") else root_path.rsplit("/", 1)[0] + "/"

    def path_and_query(self, url: str) -> str:
        parts = urlsplit(url)
        path = parts.path or "/"
        return f"{path}?{parts.query}" if parts.query else path

    def evaluate(self, url: str, depth: int) -> str | None:
        """Return a skip reason, or None when the URL passes every filter."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return SkipReason.SCHEME

        host = (parts.hostname or "").lower()
        if not self.allow_external_links:
            if self.include_subdomains:
                if registrable_domain(url) != self.root_domain:
                    return SkipReason.EXTERNAL_HOST
            elif not is_subdomain_of(host, self.root_host) or (
                host != self.root_host and not self.include_subdomains
            ):
                return SkipReason.EXTERNAL_HOST

        if depth > self.max_depth:
            return SkipReason.DEPTH

        if has_skipped_extension(url):
            return SkipReason.EXTENSION

        target = self.path_and_query(url)

        # allowBackwardLinks permits crawling above the starting path.
        if not self.allow_backward_links and host == self.root_host:
            path = parts.path or "/"
            if not path.startswith(self.root_path) and self.root_path != "/":
                return SkipReason.BACKWARD

        # Exclude wins where both match.
        if any(rx.search(target) for rx in self._exclude):
            return SkipReason.EXCLUDE_PATH
        if self._include and not any(rx.search(target) for rx in self._include):
            return SkipReason.INCLUDE_PATH

        return None


@dataclass
class DiscoveredLink:
    url: str
    depth: int
    parent_url: str | None
    discovered_via: str
    skip_reason: str | None = None

    def to_frontier_row(self, *, drop_query: bool = False) -> dict[str, object]:
        return {
            "url": self.url,
            "url_hash": url_hash(self.url),
            "normalized_hash": normalized_hash(self.url, drop_query=drop_query),
            "depth": self.depth,
            "parent_url": self.parent_url,
            "discovered_via": self.discovered_via,
            "status": "skipped" if self.skip_reason else "pending",
            "skip_reason": self.skip_reason,
        }


# --------------------------------------------------------------------------
# Honeypot avoidance (05-block-detection.md section 6)
# --------------------------------------------------------------------------


def is_honeypot_anchor(node: object) -> bool:
    """A hidden link is a trap; a nofollow link on its own is not.

    nofollow alone is common on perfectly legitimate links, so it only counts
    in combination with hidden styling.
    """
    attributes = getattr(node, "attributes", {}) or {}
    style = (attributes.get("style") or "").lower()
    classes = (attributes.get("class") or "").lower().split()
    rel = (attributes.get("rel") or "").lower()

    hidden_by_style = bool(_HIDDEN_STYLE.search(style))
    hidden_by_class = any(c in _HIDDEN_CLASSES for c in classes)

    if hidden_by_style or hidden_by_class:
        return True

    # A zero-size or 1x1 anchor is a trap regardless of styling.
    width = (attributes.get("width") or "").strip()
    height = (attributes.get("height") or "").strip()
    if width in ("0", "1") and height in ("0", "1"):
        return True

    # nofollow only matters alongside hiding, which the checks above cover;
    # this branch catches inline aria-hidden markup used the same way.
    return "nofollow" in rel and attributes.get("aria-hidden") == "true"


# --------------------------------------------------------------------------
# Link extraction
# --------------------------------------------------------------------------


def extract_links(
    html: str,
    base_url: str,
    policy: CrawlPolicy,
    *,
    depth: int,
) -> list[DiscoveredLink]:
    """Every link on a page, evaluated against the crawl policy.

    Honeypots and policy-failing URLs come back with a skip_reason so they can
    be recorded rather than silently dropped.
    """
    tree = HTMLParser(html)
    seen: set[str] = set()
    out: list[DiscoveredLink] = []

    # rel="next" is how a listing says "there is a page after this one", in the
    # <head> where no anchor scan sees it, or on an anchor that reads "›". A
    # crawl that only follows anchors stops at page one of every paginated
    # index (audit, 6 Sep 2026).
    for anchor in tree.css('link[rel~="next"][href], a[rel~="next"][href], a[href]'):
        href = (anchor.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        try:
            absolute = urljoin(base_url, href)
        except ValueError:
            continue
        absolute = absolute.split("#", 1)[0]
        if not absolute.startswith(("http://", "https://")):
            continue
        # Cloudflare's own endpoints under the site's host — email
        # obfuscation, challenge platform, beacons. Never content. Screened
        # here at the anchor, so they reach neither the frontier nor the
        # page's `links` (measured: six per homepage on a Framer site).
        if "/cdn-cgi/" in absolute:
            continue

        canonical = normalize_url(absolute, drop_query=policy.ignore_query_parameters)
        if canonical in seen:
            continue
        seen.add(canonical)

        # Cheap and prevents a self-inflicted wound: following a hidden link
        # is how a crawler announces itself and gets its session downgraded.
        if is_honeypot_anchor(anchor):
            out.append(DiscoveredLink(absolute, depth + 1, base_url, "crawl", SkipReason.HONEYPOT))
            continue

        reason = policy.evaluate(absolute, depth + 1)
        out.append(DiscoveredLink(absolute, depth + 1, base_url, "crawl", reason))

    return out


# --------------------------------------------------------------------------
# Sitemaps
# --------------------------------------------------------------------------


def sitemap_urls_from_robots(robots_body: str) -> list[str]:
    out: list[str] = []
    for line in robots_body.splitlines():
        cleaned = line.split("#", 1)[0].strip()
        if cleaned.lower().startswith("sitemap:"):
            value = cleaned.split(":", 1)[1].strip()
            if value.startswith(("http://", "https://")):
                out.append(value)
    return out


def parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """Return (page urls, nested sitemap urls).

    A sitemap index points at further sitemaps; the caller recurses, bounded.
    """
    try:
        root = SafeElementTree.fromstring(xml)
    except (DefusedXmlException, ParseError, ValueError):
        # A malformed or hostile sitemap yields no URLs rather than raising:
        # one bad sitemap must not fail the crawl that referenced it.
        return [], []

    tag = root.tag.rsplit("}", 1)[-1].lower()
    locs = [
        (element.text or "").strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1].lower() == "loc" and element.text
    ]
    locs = [loc for loc in locs if loc.startswith(("http://", "https://"))]

    if tag == "sitemapindex":
        return [], locs
    return locs, []


def default_sitemap_urls(root_url: str) -> list[str]:
    parts = urlsplit(root_url)
    base = f"{parts.scheme}://{parts.netloc}"
    return [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"]


def title_from_html(html: str) -> str | None:
    tree = HTMLParser(html)
    node = tree.css_first("title")
    if node is None:
        return None
    text = node.text(strip=True)
    return re.sub(r"\s+", " ", text) if text else None


__all__ = [
    "CrawlPolicy",
    "DiscoveredLink",
    "SkipReason",
    "default_sitemap_urls",
    "extract_links",
    "host_of",
    "is_honeypot_anchor",
    "parse_sitemap",
    "sitemap_urls_from_robots",
    "title_from_html",
    "_SITEMAP_NS",
]
