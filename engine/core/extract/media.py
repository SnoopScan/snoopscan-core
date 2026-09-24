"""Every image, video and audio file a page references.

Asked for directly: competitors return media and we returned none at all. A
scrape of the Red Panda article — a page made largely of photographs — came
back with zero image references, because trafilatura is called with
`include_images=False` and the link collector only reads `<a href>`.

This costs nothing to add and nothing to run. A media URL comes from the
HTML, so listing it does NOT require downloading the file: the bytes stay
blocked at the network layer and the caller still gets every URL. The two
were never in tension.

Lazy-loaded media is the normal case rather than the exception, so `src` is
only the first place to look — a page that lazy-loads every photograph would
otherwise report none of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

# In priority order. `src` first when it is real, then the lazy-loader
# attributes, which are an OPEN set — every framework invents its own — so
# this is a fast path, not a promise of completeness, and `srcset` below
# catches responsive images whatever attribute carried them.
_URL_ATTRS = ("src", "data-src", "data-original", "data-lazy-src", "data-url")

_SKIP_PREFIXES = ("data:", "blob:", "javascript:", "about:")


@dataclass(frozen=True)
class MediaItem:
    url: str
    type: str  # image | video | audio
    alt: str | None = None


def _first_url(node: object, base_url: str) -> str | None:
    # node is `object` here (called on both selectolax Nodes and other
    # shapes), so getattr's return is Any — annotated explicitly so that
    # Any does not poison every read below into an unchecked return type.
    attrs: dict[str, str | None] = getattr(node, "attributes", {}) or {}
    for name in _URL_ATTRS:
        raw = (attrs.get(name) or "").strip()
        if raw and not raw.startswith(_SKIP_PREFIXES):
            try:
                absolute = urljoin(base_url, raw)
            except ValueError:
                continue
            if absolute.startswith(("http://", "https://")):
                return absolute

    # A responsive image often carries no usable src at all, only a srcset of
    # "url 320w, url 640w" pairs. Take the LAST, which is the largest.
    raw_set = (attrs.get("srcset") or attrs.get("data-srcset") or "").strip()
    if raw_set:
        candidates = [c.strip().split(" ")[0] for c in raw_set.split(",") if c.strip()]
        for candidate in reversed(candidates):
            if candidate and not candidate.startswith(_SKIP_PREFIXES):
                try:
                    absolute = urljoin(base_url, candidate)
                except ValueError:
                    continue
                if absolute.startswith(("http://", "https://")):
                    return absolute
    return None


def collect_media(html: str, base_url: str) -> list[MediaItem]:
    """Absolute-resolved, de-duplicated, order-stable — as collect_links is."""
    tree = HTMLParser(html)
    seen: set[str] = set()
    out: list[MediaItem] = []

    # A <video poster> is the still frame, so it is an IMAGE that happens to
    # hang off a video element — typing it "video" would hand callers a JPEG
    # where they asked for footage.
    for node in tree.css("video[poster]"):
        raw = (node.attributes.get("poster") or "").strip()
        if raw and not raw.startswith(_SKIP_PREFIXES):
            try:
                poster_url = urljoin(base_url, raw)
            except ValueError:
                poster_url = ""
            if poster_url.startswith(("http://", "https://")) and poster_url not in seen:
                seen.add(poster_url)
                out.append(MediaItem(url=poster_url, type="image", alt=None))

    for selector, kind in (
        ("img", "image"),
        ("picture source", "image"),
        ("video", "video"),
        ("video source", "video"),
        ("audio", "audio"),
        ("audio source", "audio"),
    ):
        for node in tree.css(selector):
            url = _first_url(node, base_url)
            if url is None or url in seen:
                continue
            seen.add(url)
            alt = (node.attributes.get("alt") or "").strip() if kind == "image" else ""
            out.append(MediaItem(url=url, type=kind, alt=alt or None))

    return out
