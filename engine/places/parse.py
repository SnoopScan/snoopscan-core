"""Parse the rendered Google Maps results page and a place's detail panel.

Maps' class names are obfuscated and rotate; its ARIA does not — a results
list is a `div[role=article]` per place with the name as the link's
`aria-label`, the rating as `span[role=img][aria-label="4.4 stars"]`, and the
rest as text runs separated by `·`. The detail panel labels its facts
outright: `Address: …`, `Website: …`, `Phone: …`. Everything here anchors on
those, never on a class.

The place link carries the identity: `!1s0x…:0x…` is the feature id (the key
for dedupe and for the internal endpoints later), `!3d…!4d…` the coordinates.

Fixtures trimmed from live renders live in engine/tests/fixtures/; a Maps
markup change is a failing test there, not silent nulls in a customer's list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

from selectolax.parser import HTMLParser, Node

from engine.places.models import Place

_FEATURE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)")
_LATLNG = re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)")
_RATING = re.compile(r"^\s*(\d(?:\.\d)?)\s+stars?", re.I)
_REVIEWS = re.compile(r"([\d,]+)\s+reviews?", re.I)
_OPEN_WORDS = ("open", "closed", "closes", "opens")


@dataclass(frozen=True)
class PlaceIdentity:
    feature_id: str | None
    latitude: float | None
    longitude: float | None


def decode_place_url(href: str) -> PlaceIdentity:
    """Feature id and coordinates from a /maps/place/... link."""
    raw = unquote(href)
    fid = _FEATURE.search(raw)
    ll = _LATLNG.search(raw)
    return PlaceIdentity(
        feature_id=fid.group(1) if fid else None,
        latitude=float(ll.group(1)) if ll else None,
        longitude=float(ll.group(2)) if ll else None,
    )


def parse_results(html: str) -> list[Place]:
    """Every place on a rendered results page, in page order, deduplicated on
    feature id. A block without a place link or an id is not a place."""
    tree = HTMLParser(html)
    out: list[Place] = []
    seen: set[str] = set()
    for block in tree.css('div[role="article"]'):
        link = block.css_first('a[href*="/maps/place/"]')
        if link is None:
            continue
        href = link.attributes.get("href") or ""
        ident = decode_place_url(href)
        if not ident.feature_id or ident.feature_id in seen:
            continue
        seen.add(ident.feature_id)
        name = (link.attributes.get("aria-label") or link.text() or "").strip()
        rating, reviews = _rating_of(block)
        category, address, tagline, open_status = _text_facts(block, name)
        out.append(
            Place(
                feature_id=ident.feature_id,
                name=name,
                place_url=href.split("?", 1)[0],
                latitude=ident.latitude,
                longitude=ident.longitude,
                rating=rating,
                review_count=reviews,
                category=category,
                address=address,
                tagline=tagline,
                open_status=open_status,
            )
        )
    return out


def _rating_of(block: Node) -> tuple[float | None, int | None]:
    for node in block.css("[aria-label]"):
        label = node.attributes.get("aria-label") or ""
        m = _RATING.match(label)
        if m:
            r = _REVIEWS.search(label)
            return float(m.group(1)), int(r.group(1).replace(",", "")) if r else None
    return None, None


def _text_facts(block: Node, name: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Category, address, tagline and open-status from the block's text runs.

    The runs come in a fixed order — name, rating, `category · address`,
    tagline, open-status — but any of the last three can be absent, so each is
    identified by shape rather than position: the address has a digit or a
    street word, the open-status starts with Open/Closed, the tagline is what
    is left.
    """
    runs = [t.strip() for t in block.text(separator="\n").split("\n")]
    runs = [t for t in runs if t and t != "·" and t != name and not _is_glyph(t)]
    # Drop the bare rating number and the "4.4 stars" duplicate.
    runs = [t for t in runs if not re.fullmatch(r"\d(?:\.\d)?", t)]

    category = address = tagline = open_status = None
    for run in runs:
        low = run.lower()
        looks_open = any(low.startswith(w) or low.startswith("· " + w) for w in _OPEN_WORDS)
        if open_status is None and looks_open:
            open_status = run.lstrip("· ").strip()
            continue
        if category is None and address is None and len(run) <= 40 and not re.search(r"\d", run):
            category = run
            continue
        if address is None and re.search(r"\d", run) and len(run) <= 80:
            address = run
            continue
        if tagline is None and len(run) <= 120:
            tagline = run
    # A block that carried only "Closes soon· 1 pm · Opens 8 am Sun" style text
    # may have folded the status into two runs; keep the first.
    return category, address, tagline, open_status


def _is_glyph(text: str) -> bool:
    """Material icon codepoints render as private-use characters in text."""
    return all(0xE000 <= ord(ch) <= 0xF8FF for ch in text)


# --------------------------------------------------------------------------
# The detail panel
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaceDetail:
    address: str | None = None
    website: str | None = None
    phone: str | None = None
    rating: float | None = None
    review_count: int | None = None


def parse_detail(html: str) -> PlaceDetail:
    """Website, phone and full address from a place's detail panel."""
    tree = HTMLParser(html)
    address = website = phone = None
    rating = reviews = None
    for node in tree.css("[aria-label]"):
        label = (node.attributes.get("aria-label") or "").strip()
        if label.startswith("Address:") and address is None:
            address = label.split(":", 1)[1].strip()
        elif label.startswith("Website:") and website is None:
            href = node.attributes.get("href")
            website = href or _bare_website(label.split(":", 1)[1].strip())
        elif label.startswith("Phone:") and phone is None:
            phone = label.split(":", 1)[1].strip()
        elif rating is None:
            m = _RATING.match(label)
            if m:
                rating = float(m.group(1))
                r = _REVIEWS.search(label)
                reviews = int(r.group(1).replace(",", "")) if r else None
    if website is None:
        authority = tree.css_first('a[data-item-id="authority"]')
        if authority is not None:
            website = authority.attributes.get("href")
    return PlaceDetail(
        address=address, website=website, phone=phone, rating=rating, review_count=reviews
    )


def _bare_website(text: str) -> str | None:
    """`joscoffee.com` → `https://joscoffee.com`; anything odd → None."""
    text = text.strip().rstrip("/")
    if not text or " " in text or "." not in text:
        return None
    return text if text.startswith("http") else f"https://{text}"
