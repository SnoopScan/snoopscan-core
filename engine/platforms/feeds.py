"""RSS and Atom: discovery from <link rel="alternate">, then the well-known
paths, parsed with defusedxml. Every blog platform speaks one of these, so a
feed is the universal fallback when there is no JSON API to ask."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from defusedxml import ElementTree as ET

from engine.platforms.models import Post

_ALT_RE = re.compile(
    r'<link[^>]+rel=["\']alternate["\'][^>]+'
    r'type=["\']application/(?:rss|atom)\+xml["\'][^>]*>',
    re.I,
)
_HREF_RE = re.compile(r'href=["\']([^"\']+)', re.I)
WELL_KNOWN = (
    "/feed/",
    "/feed",
    "/rss/",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/atom.xml",
    "/index.xml",
)

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def declared_feeds(html: str, base_url: str) -> list[str]:
    out: list[str] = []
    for tag in _ALT_RE.findall(html[:300_000]):
        m = _HREF_RE.search(tag)
        if m:
            out.append(urljoin(base_url, m.group(1)))
    return out


def looks_like_feed(body: bytes) -> bool:
    head = body[:600].lstrip().lower()
    early = body[:2000].lower()
    if head.startswith((b"<rss", b"<feed")):
        return True
    return head.startswith(b"<?xml") and (b"<rss" in early or b"<feed" in early)


def _text(el, *paths: str) -> str | None:
    for p in paths:
        node = el.find(p, _NS)
        if node is not None:
            txt = (node.text or "").strip() if node.text else ""
            if not txt and node.attrib.get("href"):
                txt = node.attrib["href"]
            if txt:
                return txt
    return None


def _tags(it) -> list[str]:  # type: ignore[no-untyped-def]
    rss = [c.text.strip() for c in it.findall("category") if c.text]
    if rss:
        return rss
    return [c.attrib["term"] for c in it.findall("atom:category", _NS) if c.attrib.get("term")]


def parse_feed(xml: str, platform: str = "feed") -> list[Post]:
    """RSS 2.0 <item> and Atom <entry>, into Posts. Tolerant: a missing field is
    None, a broken document is an empty list, never an exception."""
    try:
        root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
    except Exception:  # noqa: BLE001 - a broken feed is not a crash
        return []
    posts: list[Post] = []
    items = (
        root.findall(".//item")
        or root.findall("atom:entry", _NS)
        or root.findall("{http://www.w3.org/2005/Atom}entry")
    )
    for it in items:
        title = _text(it, "title", "atom:title") or ""
        link = _text(it, "link", "atom:link") or ""
        if not link:
            for lnk in it.findall("atom:link", _NS):
                if lnk.attrib.get("rel", "alternate") == "alternate" and lnk.attrib.get("href"):
                    link = lnk.attrib["href"]
                    break
        if not (title and link):
            continue
        posts.append(
            Post(
                platform=platform,
                id=_text(it, "guid", "atom:id") or link,
                title=title,
                url=link,
                published_at=_text(it, "pubDate", "atom:published", "dc:date", "atom:updated"),
                updated_at=_text(it, "atom:updated"),
                author=_text(it, "dc:creator", "author", "atom:author/atom:name"),
                excerpt=_text(it, "description", "atom:summary"),
                content_html=_text(it, "content:encoded", "atom:content"),
                tags=_tags(it),
            )
        )
    return posts


__all__ = ["WELL_KNOWN", "declared_feeds", "looks_like_feed", "parse_feed"]
