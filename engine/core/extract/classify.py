"""Page classification (04-extraction.md section 2).

Cheap, deterministic, rule-based. No LLM. Runs before extraction so the router
can pick the right extractor — heuristic extractors score ~0.55 F1 on forums
and ~0.68 on product pages, so routing everything through one is the single
biggest quality loss available.

JSON-LD and microdata are the strongest signals and the cheapest to read.
Structured markup is parsed FIRST: many sites hand you exactly what you want in
a <script type="application/ld+json"> block and everyone ignores it.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from selectolax.parser import HTMLParser, Node

from engine.core.models import PageType


@dataclass
class Classification:
    page_type: PageType
    confidence: float
    scores: dict[str, float] = field(default_factory=dict)
    structured_data: list[dict[str, Any]] = field(default_factory=list)
    repeated_selector: str | None = None
    repeated_count: int = 0
    # The matching nodes themselves, so a later stage can ask whether the group
    # survived extraction. Excluded from equality/repr: they are DOM handles,
    # not part of the classification's identity.
    repeated_nodes: list[Any] = field(default_factory=list, repr=False, compare=False)
    # EVERY group that met the repetition threshold, winner included.
    repeated_groups: list[list[Any]] = field(default_factory=list, repr=False, compare=False)


_TIMESTAMP_HINTS = re.compile(
    r"\b(\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"[a-z]*\s+\d{1,2}|\d+\s+(?:minute|hour|day|week|month|year)s?\s+ago)\b",
    re.IGNORECASE,
)
_AUTHOR_HINTS = re.compile(r"\b(posted by|author|by\s+\w+|replied|wrote|#\d+)\b", re.IGNORECASE)
_PRICE_HINTS = re.compile(r"[$£€¥]\s?\d|(?:\d+[.,]\d{2})\s?(?:USD|GBP|EUR)", re.IGNORECASE)

# Authorship and timestamp markup, as forums actually emit it.
_AUTHOR_MARKUP = (
    '[itemprop="author"], [rel="author"], [class*="author"], [class*="username"], '
    '[class*="user-name"], [class*="poster"], .byline'
)
_TIME_MARKUP = 'time, [datetime], [class*="timestamp"], [class*="posted"]'


def parse_json_ld(tree: HTMLParser) -> list[dict[str, Any]]:
    """Every JSON-LD object on the page, flattened out of @graph wrappers."""
    out: list[dict[str, Any]] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(deep=True, strip=True)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            if isinstance(graph, list):
                out.extend(item for item in graph if isinstance(item, dict))
            else:
                out.append(candidate)
    return out


def _ld_types(blocks: list[dict[str, Any]]) -> set[str]:
    types: set[str] = set()
    for block in blocks:
        value = block.get("@type")
        if isinstance(value, str):
            types.add(value.lower())
        elif isinstance(value, list):
            types.update(str(v).lower() for v in value)
    return types


def structural_signature(node: Node) -> str:
    """Tag path plus class set, ignoring text.

    Two sibling posts in a forum, or two product cards in a listing, share a
    signature; their text does not.
    """
    parts: list[str] = []
    current: Node | None = node
    depth = 0
    while current is not None and depth < 4:
        tag = current.tag or "?"
        classes = current.attributes.get("class") or ""
        normalised = ".".join(sorted(c for c in classes.split() if not c.isdigit()))
        parts.append(f"{tag}[{normalised}]")
        current = current.parent
        depth += 1
    return ">".join(reversed(parts))


def find_repeated_blocks(
    tree: HTMLParser, min_count: int = 3, qualifying: list[list[Node]] | None = None
) -> tuple[str | None, int, list[Node]]:
    """The dominant repeating unit: a post, a product card, a listing row.

    Returns the winning signature, its count, and the matching nodes. Pass
    `qualifying` to also collect EVERY group that met `min_count`, which is what
    lets a caller notice that a losing group was dropped from the output.
    """
    qualifying = [] if qualifying is None else qualifying
    buckets: dict[str, list[Node]] = {}
    seen_text: dict[str, set[str]] = {}
    for node in tree.css("div, li, article, section, tr, td"):
        text = node.text(deep=True, strip=True) or ""
        # A repeating unit must carry meaningful text, otherwise layout
        # wrappers win by sheer count.
        if len(text) < 40:
            continue
        sig = structural_signature(node)
        # The same text twice is DUPLICATION, not repetition. Framer (and
        # Webflow) emit every section once per breakpoint, `aria-hidden` on all
        # but one; three copies of "What We Stand For" are one section, not
        # three forum posts. Measured on a Framer about-page, Sep 2026: the
        # copies made the page a "forum", the forum had no timestamps, and a
        # real 200 with 743 words was reported as a soft block.
        fingerprint = text[:400]
        if fingerprint in seen_text.setdefault(sig, set()):
            continue
        seen_text[sig].add(fingerprint)
        buckets.setdefault(sig, []).append(node)

    best_sig: str | None = None
    best_nodes: list[Node] = []
    best_score = 0.0
    # Every qualifying group, not just the winner. The scoring below rewards
    # total text, so a handful of long testimonials beats fifteen short product
    # cards — and on a directory page the cards are the content. A later stage
    # needs to see the groups that LOST in order to notice one was thrown away.
    qualifying.clear()
    for sig, nodes in buckets.items():
        if len(nodes) < min_count:
            continue
        qualifying.append(nodes)
        lengths = [len(n.text(deep=True, strip=True) or "") for n in nodes]
        mean = sum(lengths) / len(lengths)
        # Favour many instances of comparable size — real repeating units are
        # similar in length; accidental matches are not.
        spread = max(lengths) / mean if mean else 99.0
        score = len(nodes) * mean / max(spread, 1.0)
        if score > best_score:
            best_score, best_sig, best_nodes = score, sig, nodes
    return best_sig, len(best_nodes), best_nodes


_BUY_WORDS = (
    "add to cart",
    "add to basket",
    "add to bag",
    "buy now",
    "in den warenkorb",
    "ajouter au panier",
)


def _has_buy_button(tree: HTMLParser) -> bool:
    ids = "#add-to-cart-button, #buy-now-button, button[name='add'], form[action*='/cart/add']"
    if tree.css_first(ids):
        return True
    for node in tree.css("button, input[type='submit'], a.button, [role='button']")[:300]:
        raw = node.text() if node.tag != "input" else node.attributes.get("value")
        label = (raw or "").strip().lower()
        if any(w in label for w in _BUY_WORDS):
            return True
    return False


def classify(html: str) -> Classification:
    tree = HTMLParser(html)

    # Structured markup is read BEFORE scripts are stripped. Parsing it after
    # the strip is how JSON-LD gets silently thrown away — the exact failure
    # the spec calls out, since many sites hand you what you want in a
    # <script type="application/ld+json"> and everyone ignores it.
    ld_blocks = parse_json_ld(tree)

    for node in tree.css("script, style, noscript, svg"):
        node.decompose()

    # defaultdict rather than Counter: the rule weights are floats, and
    # Counter is typed (and behaves) as an integer multiset.
    scores: defaultdict[str, float] = defaultdict(float)
    ld_types = _ld_types(ld_blocks)

    # --- Structured markup: strongest and cheapest signal ------------------
    # ProductGroup is what Shopify emits now (a product with variants); a
    # classifier that only knew Product read Allbirds' product pages as forums.
    if ld_types & {"product", "productgroup", "offer", "aggregateoffer"}:
        scores[PageType.PRODUCT] += 5.0
    if ld_types & {"article", "newsarticle", "blogposting", "techarticle"}:
        scores[PageType.ARTICLE] += 4.0
    if ld_types & {"discussionforumposting", "qapage", "question"}:
        scores[PageType.FORUM] += 5.0
    # A page that declares itself a collection IS one, however repetitive its
    # product cards look to the forum heuristic (which scores 4.0 below).
    if ld_types & {"itemlist", "collectionpage", "offercatalog", "searchresultspage"}:
        scores[PageType.LISTING] += 5.0
    if ld_types & {"techarticle", "apireference", "howto"}:
        scores[PageType.DOCS] += 2.0

    if tree.css_first('[itemtype*="schema.org/Product"]'):
        scores[PageType.PRODUCT] += 3.0
    # og:type=product is set per product page, never copied across an index
    # the way og:type=article is, so it needs no corroboration.
    if tree.css_first('meta[property="og:type"][content="product"]'):
        scores[PageType.PRODUCT] += 4.5
    # A buy button. Amazon declares no schema at all and read as a forum (the
    # Q&A and review blocks repeat); "Add to Cart" on the page is the plainest
    # statement of what the page is for.
    if _has_buy_button(tree):
        scores[PageType.PRODUCT] += 3.0

    # --- Text and link measurements ---------------------------------------
    body = tree.body or tree.root
    full_text = (body.text(deep=True, strip=True) if body else "") or ""
    text_len = max(len(full_text), 1)
    links = tree.css("a")
    link_text_len = sum(len(a.text(deep=True, strip=True) or "") for a in links)
    link_ratio = link_text_len / text_len

    headings = tree.css("h1")
    paragraphs = tree.css("p")
    tables = tree.css("table")
    code_blocks = tree.css("pre, code")

    # --- Article ----------------------------------------------------------
    # ONE <article> is an article. Many <article> elements is an index of
    # articles, which is a listing — news homepages mark up every teaser card
    # that way. Measured on a live news homepage: 46 <article> elements were
    # scored as a single article, routed to the single-article extractor, and
    # yielded 355 characters out of 125,317 available. The count is the
    # signal, not the presence.
    article_elements = len(tree.css("article"))
    if article_elements == 1:
        scores[PageType.ARTICLE] += 2.0
    elif article_elements >= 4:
        scores[PageType.LISTING] += 2.5

    if len(headings) == 1:
        scores[PageType.ARTICLE] += 1.0
    if link_ratio < 0.25 and len(paragraphs) >= 4:
        scores[PageType.ARTICLE] += 2.0
    # og:type only describes a single article; on an index page it is stale
    # metadata copied across the template, so it needs corroboration.
    if tree.css_first('meta[property="og:type"][content="article"]') and (article_elements <= 1):
        scores[PageType.ARTICLE] += 2.0

    # --- Docs -------------------------------------------------------------
    if code_blocks:
        scores[PageType.DOCS] += min(len(code_blocks) * 0.4, 2.5)
    if tree.css_first("nav ul li ul, aside ul li ul, .sidebar ul li ul"):
        scores[PageType.DOCS] += 1.5
    if tree.css_first('[class*="breadcrumb"], [aria-label*="readcrumb"]'):
        scores[PageType.DOCS] += 0.8

    # --- Repeated blocks: forum vs listing --------------------------------
    groups: list[list[Node]] = []
    signature, count, nodes = find_repeated_blocks(tree, qualifying=groups)
    if count >= 3:
        sample = " ".join((n.text(deep=True, strip=True) or "")[:400] for n in nodes[:6])
        # Markup is the reliable signal here: a forum marks its authors and
        # timestamps up (class="author", rel="author", <time datetime>) even
        # when the visible text is just a username with no "posted by" prose.
        markup_authors = sum(1 for n in nodes if n.css_first(_AUTHOR_MARKUP))
        markup_times = sum(1 for n in nodes if n.css_first(_TIME_MARKUP))
        has_time = bool(_TIMESTAMP_HINTS.search(sample)) or markup_times >= count * 0.5
        has_author = bool(_AUTHOR_HINTS.search(sample)) or markup_authors >= count * 0.5
        block_images = sum(1 for n in nodes if n.css_first("img"))
        avg_len = sum(len(n.text(deep=True, strip=True) or "") for n in nodes) / len(nodes)

        if has_time and has_author:
            scores[PageType.FORUM] += 4.0
        elif has_time:
            # A timestamp per block is weak evidence on its own. An author hint
            # on its own is none: `_AUTHOR_HINTS` matches "by the numbers" and
            # "by design" in ordinary marketing copy, and on the measured site that was
            # enough to call an About page a forum (5 Sep 2026).
            scores[PageType.FORUM] += 0.75

        # Cards: linked, image-bearing, short. Posts: prose, long.
        if block_images >= count * 0.5 and avg_len < 400:
            scores[PageType.LISTING] += 3.0
        elif avg_len < 250 and count >= 6:
            scores[PageType.LISTING] += 2.0

    if _PRICE_HINTS.search(full_text[:5_000]):
        scores[PageType.PRODUCT] += 1.5
        if count >= 4:
            scores[PageType.LISTING] += 1.0

    # --- Table-dominant ---------------------------------------------------
    for table in tables:
        rows = table.css("tr")
        if table.css_first("th") and len(rows) > 3:
            table_text = len(table.text(deep=True, strip=True) or "")
            if table_text / text_len > 0.4:
                scores[PageType.TABLE] += 4.0
                break

    if not scores:
        return Classification(
            page_type=PageType.UNKNOWN,
            confidence=0.0,
            structured_data=ld_blocks,
            repeated_selector=signature,
            repeated_count=count,
            repeated_nodes=nodes,
            repeated_groups=groups,
        )

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_type, top_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

    # A tie resolves to unknown, which takes the heuristic path.
    if top_score == runner_up:
        return Classification(
            page_type=PageType.UNKNOWN,
            confidence=0.0,
            scores=dict(scores),
            structured_data=ld_blocks,
            repeated_selector=signature,
            repeated_count=count,
            repeated_nodes=nodes,
            repeated_groups=groups,
        )

    total = sum(scores.values())
    confidence = top_score / total if total else 0.0
    return Classification(
        page_type=PageType(top_type),
        confidence=round(confidence, 3),
        scores=dict(scores),
        structured_data=ld_blocks,
        repeated_selector=signature,
        repeated_count=count,
        repeated_nodes=nodes,
        repeated_groups=groups,
    )
