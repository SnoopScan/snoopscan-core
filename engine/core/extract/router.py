"""Extraction router (04-extraction.md section 1).

Routes by detected page type: heuristic for articles/docs/unknown, structured
for forums/products/listings/tables. Deliberately pluggable — adding a
model-backed extractor later must be registering a handler for a page type, not
a refactor.

Pipeline:
    raw HTML -> pre-clean -> selector filter -> classify -> route
             -> convert -> post-clean -> score
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from engine.core.extract import boilerplate as bp
from engine.core.extract.classify import Classification, classify
from engine.core.extract.confidence import ConfidenceInputs, score
from engine.core.extract.media import collect_media
from engine.core.extract.text import node_text
from engine.core.models import ExtractionPath, PageType

_WORD = re.compile(r"\b[\w'-]+\b", re.UNICODE)


@dataclass
class ExtractionResult:
    markdown: str = ""
    html: str = ""
    links: list[str] = field(default_factory=list)
    media: list[Any] = field(default_factory=list)
    structured: dict[str, Any] | None = None
    title: str | None = None
    description: str | None = None
    language: str | None = None
    author: str | None = None
    published_at: str | None = None
    page_type: PageType = PageType.UNKNOWN
    extraction_path: ExtractionPath = ExtractionPath.HEURISTIC
    word_count: int = 0
    confidence: float = 0.0
    classification: Classification | None = None

    @property
    def char_count(self) -> int:
        return len(self.markdown)


@dataclass
class ExtractOptions:
    only_main_content: bool = True
    include_tags: list[str] = field(default_factory=list)
    exclude_tags: list[str] = field(default_factory=list)
    include_links: bool = True
    include_media: bool = True
    remove_base64_images: bool = True
    baseline_mean: int | None = None
    baseline_stdev: int | None = None


class Extractor(Protocol):
    """A handler for one or more page types. Register in `ROUTES`."""

    name: str

    def extract(
        self, html: str, url: str, cls: Classification, options: ExtractOptions
    ) -> ExtractionResult: ...


# --------------------------------------------------------------------------
# Steps 1 and 2 — pre-clean and selector filtering
# --------------------------------------------------------------------------

_STRIP_TAGS = ("script", "style", "noscript", "svg", "iframe", "template")
# `hidden` and aria-hidden="true" are the two ways markup says "not shown"; a
# closed <dialog> is a third; role="dialog"/"alertdialog" is the fourth and the
# commonest — Allbirds keeps its refund and privacy policies in
# <div role="dialog" id="modal-…"> on every product page, 39,000 characters
# each, and they came back as the product's content. Inline display:none is
# left alone: too many sites toggle it in scripts for it to mean anything in
# the static source.
_HIDDEN_SELECTOR = (
    '[hidden], [aria-hidden="true"], dialog:not([open]), [role="dialog"], [role="alertdialog"]'
)
_BASE64_IMG = re.compile(r'<img[^>]+src=["\']data:image/[^"\']{200,}["\'][^>]*>', re.IGNORECASE)


# `</a><a>` — a closing tag flush against an opening one, which is where text
# extractors weld the two elements' words together.
_FLUSH_TAGS = re.compile(r"(</[a-zA-Z][a-zA-Z0-9]*>)(<[a-zA-Z])")


def separate_adjacent_elements(html: str) -> str:
    """Put a space between elements that sit flush against each other.

    Minified markup writes ``<a>English nouns</a><a>FictIf characters</a>`` with
    nothing between the tags, and every text extractor that concatenates text
    nodes welds them into ``nounsFictIf``. Our own extraction handles this
    (engine.core.extract.text), but trafilatura does its own joining and we
    cannot reach inside it — measured on behindthename.com/name/sage, 21 welded
    tokens in an 866-character page (7 Sep 2026).

    This is a STRING transform, deliberately. The first version walked the DOM
    and called `insert_before(" ")`, which is the obvious way to do it and left
    the tree in a state whose serialisation consumed unbounded memory: a 928 KB
    Etsy page took the process to SIGKILL and the API down with it, twice, on
    pages the test suite never exercised. Rewriting a string cannot corrupt a
    tree, and the cost is one regex pass.

    Only closing-then-opening pairs qualify. `<div><p>` needs no separator (a
    browser gives them their own boxes anyway) and text next to markup must be
    left alone, or `<span>Name</span>berry` becomes two words.
    """
    return _FLUSH_TAGS.sub(r"\1 \2", html)


_MAIN = 'main, [role="main"]'


def _is_the_page_behind_a_modal(node: Node) -> bool:
    """aria-hidden on the page's MAIN region is a pop-up being open, not a
    statement that the page is not content.

    When a site opens a cart drawer, region picker or newsletter offer, its
    script sets aria-hidden="true" on everything else so a screen reader stays
    in the pop-up. A request that waits for window load sees the page in that
    state: a retail home page came back with <main aria-hidden="true">, and
    stripping it turned 97,000 characters of page into 0 words (Sep 2026).

    Only aria-hidden gets this benefit of the doubt, and only on <main> (or
    role=main) or an element wrapping it. `hidden`, a closed <dialog> and a
    role=dialog are genuinely not rendered; a Framer breakpoint duplicate or a
    Shopify policy drawer is not the page's main region.
    """
    attrs = node.attributes
    if "hidden" in attrs:
        return False
    if node.tag == "dialog" or (attrs.get("role") or "") in ("dialog", "alertdialog"):
        return False
    if (attrs.get("aria-hidden") or "").lower() != "true":
        return False
    return node.tag == "main" or attrs.get("role") == "main" or node.css_first(_MAIN) is not None


def preclean(html: str, options: ExtractOptions) -> str:
    """Strip non-content elements and apply caller selectors to the SOURCE DOM.

    Selector filtering is caller intent applied before extraction; the
    boilerplate sweep later is our own cleanup on the result. Keeping them
    separate is deliberate.
    """
    if options.remove_base64_images:
        html = _BASE64_IMG.sub("", html)

    tree = HTMLParser(html)
    for tag in _STRIP_TAGS:
        for node in tree.css(tag):
            # JSON-LD survives the strip: it is the single most valuable
            # signal on product, article and forum pages, and it lives inside
            # a <script> tag. Removing it here would leave the classifier and
            # the structured extractor with nothing to read.
            if tag == "script" and (node.attributes.get("type") or "").lower() == (
                "application/ld+json"
            ):
                continue
            node.decompose()
    # What a browser does not show, we do not extract. Shopify keeps every
    # policy in a hidden drawer on every product page — 13,593 words of refund
    # and privacy text came back as the "content" of a shoe (6 Sep 2026) — and
    # Framer renders each section once per breakpoint with all but one
    # aria-hidden. Stripping them here fixes both at the source rather than
    # deduplicating the symptom downstream.
    for node in tree.css(_HIDDEN_SELECTOR):
        if _is_the_page_behind_a_modal(node):
            continue
        node.decompose()
    for comment in tree.css("comment"):
        comment.decompose()

    # excludeTags wins where both match, so it is applied last.
    if options.include_tags:
        kept: list[str] = []
        for selector in options.include_tags:
            for node in tree.css(selector):
                kept.append(node.html or "")
        if kept:
            tree = HTMLParser(f"<html><body>{''.join(kept)}</body></html>")

    if options.exclude_tags:
        for selector in options.exclude_tags:
            for node in tree.css(selector):
                node.decompose()

    out = unwrap_card_tables(tree, tree.html or html)
    # After the strips, before any extractor: make word boundaries explicit.
    # On the serialised output, so script/style content is already gone.
    return separate_adjacent_elements(out)


_CARD_BLOCKS = "h1, h2, h3, h4, h5, h6, ul, ol, p, div"


def unwrap_card_tables(tree: HTMLParser, html: str) -> str:
    """A one-column table of block content is a stack of cards: say so.

    Registers and directories lay results out as a table with one cell per
    row, each cell a heading plus a list. Every extractor mangled it
    differently: trafilatura drops headings inside cells, so Companies House's
    search came back with every company's number and address but not its NAME;
    the general converter keeps the heading and breaks the row at the list, so
    the details went instead (22 Sep 2026). Unwrapped, each cell is a block
    with its own heading, which every path reads.

    A string swap on the serialised tree, like separate_adjacent_elements, and
    for the same reason: rewriting a string cannot corrupt the tree.
    """
    for table in tree.css("table"):
        # css() matches the node itself too, so one hit is this table alone.
        if len(table.css("table")) > 1:
            continue  # holds a table of its own: leave nesting to the converter
        rows = [row.css("th, td") for row in table.css("tr")]
        rows = [cells for cells in rows if cells]
        # A table of ONE cell is layout, whatever it holds: indeed.com wraps
        # each job's title, company and town in one, and every job came back
        # as a headerless one-cell markdown table.
        if sum(len(cells) for cells in rows) != 1:
            if len(rows) < 2 or any(len(cells) != 1 for cells in rows):
                continue
            if not any(cells[0].css_first(_CARD_BLOCKS) is not None for cells in rows):
                continue  # a plain one-column list of values stays a table
        source = table.html or ""
        if not source or source not in html:
            continue
        blocks = "".join(f"<div>{_inner(cells[0])}</div>" for cells in rows)
        html = html.replace(source, f"<div>{blocks}</div>", 1)
    return html


def _inner(cell: Node) -> str:
    raw = cell.html or ""
    start = raw.find(">") + 1
    end = raw.rfind("</")
    return raw[start:end] if 0 < start <= end else ""


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def collect_links(html: str, base_url: str) -> list[str]:
    """Every discovered link, absolute-resolved, de-duplicated, order-stable."""
    tree = HTMLParser(html)
    seen: set[str] = set()
    out: list[str] = []
    for anchor in tree.css("a[href]"):
        href = (anchor.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        try:
            absolute = urljoin(base_url, href)
        except ValueError:
            continue
        if absolute.startswith(("http://", "https://")) and absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


# A markdown link or image target that is not already absolute: a path, `../`,
# or a bare relative name. Schemes (https:, mailto:, tel:, data:), in-page
# anchors and `<...>` targets (how a URL with brackets is written) are left alone.
_RELATIVE_TARGET = re.compile(r"(\]\()(?![a-zA-Z][a-zA-Z0-9+.-]*:|#|//|<)([^)\s]+)(\))")
_PROTOCOL_RELATIVE = re.compile(r"(\]\()(//[^)\s]+)(\))")
_EMPTY_BULLET = re.compile(r"^[ \t]*[-*+][ \t]*\n", re.MULTILINE)
# A paragraph that is only a non-breaking space, spelled as an entity.
_NBSP_LINE = re.compile(r"^[ \t]*(?:&nbsp;|&#160;|\u00a0)+[ \t]*\n", re.MULTILINE)


def tidy_markdown(markdown: str, base_url: str) -> str:
    """Resolve relative link targets and drop empty bullets, whichever path ran.

    trafilatura keeps links as the page wrote them, so its output handed back
    `[KENNY ROOFING SOLUTIONS LIMITED](/company/12069139)`, useless to anyone
    reading the markdown away from the page, while the other paths resolved
    them (Companies House, 22 Sep 2026). An empty `<li>` became a lone "-".
    """
    if not markdown:
        return markdown
    markdown = _RELATIVE_TARGET.sub(lambda m: m[1] + urljoin(base_url, m[2]) + m[3], markdown)
    markdown = _PROTOCOL_RELATIVE.sub(lambda m: m[1] + urljoin(base_url, m[2]) + m[3], markdown)
    return _NBSP_LINE.sub("", _EMPTY_BULLET.sub("", markdown))


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


def collapse_repeats(markdown: str) -> str:
    """Drop blocks identical to one already emitted.

    Framer and Webflow render every section once per breakpoint; the extractor
    then carries each heading and paragraph two or three times, and the caller
    has to dedupe (measured). Order is preserved; only exact repeats go.

    Lives here, not in one extractor, because `_best_of` can hand back the
    OTHER path's result — and when it started doing that on the Framer fixture
    the duplicate headings came straight back (5 Sep 2026). A cleanup that
    applies to one branch of a choice is not a cleanup.
    """
    seen: set[str] = set()
    headings_seen: set[str] = set()
    fresh = False
    in_section: set[str] = set()
    out: list[str] = []
    for block in markdown.split("\n\n"):
        key = block.strip()
        if bp.is_heading(key):
            fresh = key not in headings_seen
            headings_seen.add(key)
            in_section = set()
        # The same short field under the next entry's own heading is data: a
        # register's companies are all "Active" (Companies House, 22 Sep 2026).
        field = fresh and not bp.is_heading(key) and len(key) <= bp.ENTRY_FIELD_MAX
        if key and key in seen and not (field and key not in in_section):
            continue
        if key:
            seen.add(key)
            in_section.add(key)
        out.append(block)
    return "\n\n".join(out)


# Markdown carrying this share of raw tags is not markdown. Conversion has
# failed and dumped HTML into the field a caller reads as text.
_MARKUP_SHARE = 0.25
_TAG = re.compile(r"<[a-zA-Z/][^>]{0,200}>")


def is_mostly_markup(markdown: str) -> bool:
    """Did conversion give up and emit HTML?

    behindthename.com's menu bar reached callers as 3,850 characters of
    `<table id="menubar-table">...` in the `markdown` field, identical for every
    name, and nothing flagged it — every length and word-count check passed,
    because tags are characters and words too (7 Sep 2026). The specific cause
    is fixed at source; this is the check that would have caught it whatever
    the cause, and will catch the next one.
    """
    if not markdown:
        return False
    tag_chars = sum(len(m.group(0)) for m in _TAG.finditer(markdown))
    return tag_chars / max(len(markdown), 1) > _MARKUP_SHARE


def raw_text_length(html: str) -> int:
    # Deliberately NOT node_text(): this is a volume measure feeding a
    # calibrated floor (under_extracted), not text anyone reads or matches on.
    # Injecting a separator per block boundary would inflate it a few percent
    # and move that floor for every page. Welding costs nothing to a count.
    tree = HTMLParser(html)
    body = tree.body or tree.root
    return len((body.text(deep=True, strip=True) if body else "") or "")


def _count_structure(html: str) -> tuple[int, int, int]:
    tree = HTMLParser(html)
    headings = len(tree.css("h1, h2, h3, h4, h5, h6"))
    lists = len(tree.css("ul, ol, dl"))
    tables = len(tree.css("table"))
    return headings, lists, tables


def _count_markdown_structure(markdown: str) -> tuple[int, int, int]:
    headings = len(re.findall(r"^#{1,6}\s+\S", markdown, re.MULTILINE))
    lists = len(re.findall(r"^\s*(?:[-*+]|\d+\.)\s+\S", markdown, re.MULTILINE))
    tables = len(re.findall(r"^\|.*\|\s*$", markdown, re.MULTILINE))
    # Consecutive table rows belong to one table; approximate by grouping.
    if tables:
        tables = len(re.findall(r"(?:^\|.*\|\s*$\n)+", markdown, re.MULTILINE))
    return headings, lists, tables


def _metadata_from_html(html: str) -> dict[str, str | None]:
    tree = HTMLParser(html)

    def meta(*selectors: str) -> str | None:
        for selector in selectors:
            node = tree.css_first(selector)
            if node:
                content = (node.attributes.get("content") or "").strip()
                if content:
                    return content
        return None

    title_node = tree.css_first("title")
    title = (title_node.text(strip=True) if title_node else None) or None
    og_title = meta('meta[property="og:title"]', 'meta[name="twitter:title"]')
    # Prefer the longest sensible title — <title> is often truncated or
    # suffixed with the site name, og:title usually is not.
    if og_title and (not title or len(og_title) > len(title)):
        title = og_title

    html_node = tree.css_first("html")
    language = (html_node.attributes.get("lang") if html_node else None) or None
    if language:
        language = language.split("-")[0].lower() or None

    return {
        "title": title,
        "description": meta(
            'meta[name="description"]',
            'meta[property="og:description"]',
        ),
        "language": language,
        "author": meta('meta[name="author"]', 'meta[property="article:author"]'),
        "published_at": meta(
            'meta[property="article:published_time"]',
            'meta[name="date"]',
            'meta[itemprop="datePublished"]',
        ),
    }


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------

# Page type -> extractor name. Registering a model-backed extractor later is an
# entry here, not a refactor (04-extraction.md section 7).
ROUTES: dict[PageType, str] = {
    PageType.ARTICLE: "heuristic",
    PageType.DOCS: "heuristic",
    PageType.UNKNOWN: "heuristic",
    PageType.FORUM: "structured",
    PageType.PRODUCT: "structured",
    PageType.LISTING: "structured",
    PageType.TABLE: "structured",
}

_REGISTRY: dict[str, Extractor] = {}


def register(extractor: Extractor) -> None:
    _REGISTRY[extractor.name] = extractor


def get_extractor(page_type: PageType) -> Extractor:
    name = ROUTES.get(page_type, "heuristic")
    extractor = _REGISTRY.get(name)
    if extractor is None:
        # A missing structured extractor must degrade to heuristic rather than
        # failing the request outright.
        extractor = _REGISTRY["heuristic"]
    return extractor


def extract(
    html: str,
    url: str,
    options: ExtractOptions | None = None,
) -> ExtractionResult:
    """Full pipeline: pre-clean, classify, route, convert, sweep, score."""
    options = options or ExtractOptions()
    _ensure_extractors_registered()

    cleaned = preclean(html, options)
    cls = classify(cleaned)
    extractor = get_extractor(cls.page_type)

    result = extractor.extract(cleaned, url, cls, options)
    result.markdown = bp.sweep(result.markdown)
    result.word_count = word_count(result.markdown)

    # Cross-check: an extractor that returns far less than the page visibly
    # contains has failed, whichever path it was. Routing sends listings to
    # the structured extractor, but on some listing pages the heuristic path
    # simply does better, and a correct classification is no comfort if it
    # produces a worse result. Measured: a directory homepage classified
    # (correctly) as `listing` yielded 86 words where trafilatura got 606.
    #
    # Only runs when the primary looks bad, so the common case pays nothing.
    result = _best_of(result, cleaned, url, cls, options)

    # Applied to the WINNER, whichever path that was: its links resolve and its
    # bullets say something.
    result.markdown = tidy_markdown(collapse_repeats(result.markdown or ""), url)
    result.word_count = word_count(result.markdown)

    result.page_type = cls.page_type
    result.classification = cls

    if not result.links and options.include_links:
        result.links = collect_links(cleaned, url)

    # From the ORIGINAL html, not the pre-cleaned copy: the cleaner strips
    # what a reader would not see, and a gallery hidden behind a tab is still
    # media the page references.
    if not result.media and options.include_media:
        result.media = collect_media(html, url)

    meta = _metadata_from_html(html)
    result.title = result.title or meta["title"]
    result.description = result.description or meta["description"]
    result.language = result.language or meta["language"]
    result.author = result.author or meta["author"]
    result.published_at = result.published_at or meta["published_at"]

    src_h, src_l, src_t = _count_structure(cleaned)
    out_h, out_l, out_t = _count_markdown_structure(result.markdown)
    result.confidence = score(
        ConfidenceInputs(
            extracted_chars=len(result.markdown),
            raw_text_chars=raw_text_length(cleaned),
            source_headings=src_h,
            source_lists=src_l,
            source_tables=src_t,
            output_headings=out_h,
            output_lists=out_l,
            output_tables=out_t,
            boilerplate_hits=bp.boilerplate_hits(result.markdown),
            has_title=bool(result.title),
            has_language=bool(result.language),
            has_author_or_date=bool(result.author or result.published_at),
            author_or_date_applicable=cls.page_type in (PageType.ARTICLE, PageType.FORUM),
            baseline_mean=options.baseline_mean,
            baseline_stdev=options.baseline_stdev,
        )
    )
    return result


# A result carrying less than this share of the page's visible words is
# suspect enough to be worth a second opinion. THE definition of
# "under-extracted" for the whole extraction layer: `heuristic.under_extracted`
# imports it, so the fallback chain inside one extractor and the choice
# between extractors cannot drift apart.
#
# Was 0.10, measured against MARKDOWN CHARACTERS — which counts `](https://…)`
# link syntax as if it were prose, so a page reduced to a list of four links
# scored "fine". dictionary.com's homepage returned 31 words of an 1,850-char
# page and passed both gates (5 Sep 2026): 278 markdown chars cleared 10% of
# 1,958, and the page was 42 characters short of being checked at all.
COVERAGE_FLOOR = 0.35
# Visible words are estimated from raw text length — ~6 characters a word — so
# the check costs no second parse.
CHARS_PER_WORD = 6
# Below this there is not enough text for the ratio to mean anything. Matched
# to the heuristic extractor's own floor; 2,000 left a gap that real homepages
# sat inside.
_MIN_RAW_TEXT_FOR_CHECK = 500
# The alternative has to be clearly better, not marginally — otherwise this
# just adds churn and cost.
_ALTERNATIVE_MARGIN = 1.5


def _best_of(
    primary: ExtractionResult,
    cleaned: str,
    url: str,
    cls: Classification,
    options: ExtractOptions,
) -> ExtractionResult:
    """Run the other path when the primary looks like it lost the page.

    Returns whichever produced more usable text. The loser is discarded
    silently; the winner records which path it came from in
    `extraction_path`, so a shift shows up in the fixture suite rather than
    quietly degrading output.
    """
    raw_len = raw_text_length(cleaned)
    if raw_len < _MIN_RAW_TEXT_FOR_CHECK:
        return primary
    # Words, not markdown characters: link syntax is not prose.
    thin = primary.word_count < COVERAGE_FLOOR * (raw_len // CHARS_PER_WORD)
    # Output that is mostly tags has failed regardless of how long it is.
    if is_mostly_markup(primary.markdown):
        thin = True
    # A word count is blind to the failure that matters most on a listing.
    # A grid of cards is word-POOR and information-DENSE: dropping all fifteen
    # integration cards off a directory page cost 13% of the words, so coverage
    # read 84% and the page looked healthy while its entire reason for existing
    # was gone (measured, Sep 2026). Ask a structural question instead — did we
    # keep the repeating unit the page is built from?
    if not thin and not _dropped_the_listing(primary.markdown, cls):
        return primary

    alternative_name = "heuristic" if ROUTES.get(cls.page_type) == "structured" else "structured"
    alternative = _REGISTRY.get(alternative_name)
    if alternative is None:
        return primary

    try:
        candidate = alternative.extract(cleaned, url, cls, options)
    except Exception:  # noqa: BLE001 - a second opinion must never fail the request
        return primary

    candidate.markdown = bp.sweep(candidate.markdown)
    candidate.word_count = word_count(candidate.markdown)

    # A candidate that KEEPS the listing beats a longer one that lost it: the
    # cards are what the page is for, and prose about them is not a substitute.
    # Markup is not content, and a word count cannot tell the difference: the
    # structured path emitted 71 KB of raw `<table>` for hetzner.com's product
    # matrix, which counted as 4,240 "words" and beat the heuristic path's
    # 1,528 words of actual prose (7 Sep 2026). Whatever else is true, output a
    # caller cannot read loses to output they can.
    if is_mostly_markup(primary.markdown) and not is_mostly_markup(candidate.markdown):
        return candidate
    if is_mostly_markup(candidate.markdown) and not is_mostly_markup(primary.markdown):
        return primary

    primary_lost = _dropped_the_listing(primary.markdown, cls)
    candidate_lost = _dropped_the_listing(candidate.markdown, cls)
    if primary_lost and not candidate_lost:
        return candidate
    if candidate_lost and not primary_lost:
        return primary
    # Both paths fell short of the page. After the listing check (the
    # grid is the stronger signal) and before letting length decide, ask
    # which one is about the PAGE: a software-review page's longest paragraph
    # was a promoted competitor's blurb, and word count would have handed the
    # caller an advert for a different product (Sep 2026). The heading names
    # the subject; a result that never mentions it is about something else.
    # Not when BOTH dropped a listing: that case has its own rescue below,
    # and the grid matters more than which fragment names the subject.
    subject = None if (primary_lost and candidate_lost) else _page_subject(cleaned)
    if subject:
        primary_on = subject in (primary.markdown or "").casefold()
        candidate_on = subject in (candidate.markdown or "").casefold()
        if candidate_on and not primary_on:
            return candidate
        if primary_on and not candidate_on:
            return primary

    if candidate.word_count > primary.word_count * _ALTERNATIVE_MARGIN:
        return candidate
    if primary_lost and candidate_lost:
        rescued = _whole_main_content(primary, cleaned, url, cls)
        if rescued is not None:
            return rescued
        # The rescue could not keep the grid either. Relevance is the last
        # word left: that review page counts as a listing (its competitor
        # rows repeat), both paths "lost" it, the rescue returned nothing —
        # and the fallback was the competitor's advert.
        subject = _page_subject(cleaned)
        if (
            subject
            and subject in (candidate.markdown or "").casefold()
            and subject not in (primary.markdown or "").casefold()
        ):
            return candidate
    return primary


# Words a heading uses about ANY subject. What is left is what the page is about.
_GENERIC_HEADING_WORDS = frozenset(
    {
        "reviews",
        "review",
        "product",
        "products",
        "details",
        "detail",
        "pricing",
        "features",
        "feature",
        "best",
        "top",
        "guide",
        "home",
        "welcome",
        "about",
        "overview",
        "official",
        "site",
        "page",
        "the",
        "and",
        "for",
        "with",
        "your",
        "alternatives",
        "comparison",
        "vs",
        "news",
        "blog",
        "latest",
    }
)


def _page_subject(cleaned: str) -> str | None:
    """The distinctive first word of the page's own heading, casefolded.

    From the <h1>, else the <title> before any separator. None when there is
    no heading or nothing distinctive in it — then the old rule stands.
    """
    try:
        tree = HTMLParser(cleaned)
    except Exception:  # noqa: BLE001 - a relevance hint must never fail a request
        return None
    heading = tree.css_first("h1")
    text = heading.text(strip=True) if heading is not None else ""
    if not text:
        title = tree.css_first("title")
        text = (
            re.split(r"[|:\u2013\u2014-]", title.text(strip=True))[0] if title is not None else ""
        )
    for raw in text.split():
        word = re.sub(r"[^\w]", "", raw).casefold()
        if len(word) >= 4 and not word.isdigit() and word not in _GENERIC_HEADING_WORDS:
            return word
    return None


def _whole_main_content(
    primary: ExtractionResult, cleaned: str, url: str, cls: Classification
) -> ExtractionResult | None:
    """Last resort: convert the main content node wholesale.

    Reached only when BOTH routed paths threw the page's repeating block away,
    which is the case a word count cannot see. Converting the main node keeps
    everything the page shows, prose and grid alike — worse prose-to-noise than
    a good extractor, and far better than handing back a directory page with no
    directory in it. Returned only if it actually rescues the group, so a page
    that is genuinely thin is never made noisier for nothing.
    """
    from selectolax.parser import HTMLParser

    from engine.core.extract.structured import convert_to_markdown, main_content_node

    try:
        markdown = bp.sweep(convert_to_markdown(main_content_node(HTMLParser(cleaned))))
    except Exception:  # noqa: BLE001 - a rescue must never fail the request
        return None
    if not markdown or _dropped_the_listing(markdown, cls):
        return None

    # A rescue that hands back the chrome is not a rescue. On IMDb's homepage
    # this swapped 167 words of real featured content for 322 words of nav and
    # language picker — more words, no page — and the caller was billed 5
    # credits for a menu. Measured 9 Sep 2026: 3 of 17 captures.
    from engine.core.detect.validator import is_nav_shell

    if is_nav_shell(markdown, word_count(markdown)):
        return None

    primary.markdown = markdown
    primary.word_count = word_count(markdown)
    primary.extraction_path = ExtractionPath.FALLBACK
    return primary


# A repeating unit is worth rescuing once there are enough of them to be the
# point of the page rather than an incidental trio.
_LISTING_MIN_ITEMS = 5
# How many of the group's items must survive for the listing to count as kept.
_LISTING_MIN_KEPT = 0.5


def _recognises(text: str, haystack: str) -> bool:
    """Is this item's opening recognisable in the extracted markdown?"""
    needle = " ".join((text or "").split()[:4]).casefold()
    return bool(needle) and needle in haystack


def _longest_run(node: Node) -> str:
    """The item's longest block of prose, which is what it is actually about.

    Headers get normalised by the extractors (dates especially); body text does
    not, so it is the more reliable witness that an item survived.
    """
    best = ""
    # Prose LEAVES only. Including div/span returns the whole item again,
    # which is the very text that failed to match.
    for child in node.css("p, blockquote, li, td, h1, h2, h3, h4"):
        candidate = node_text(child)
        if len(candidate) > len(best):
            best = candidate
    return best


def _dropped_the_listing(markdown: str | None, cls: Classification) -> bool:
    """Did the extraction throw away the page's dominant repeating block?

    Readability-style extraction treats link-dense blocks as navigation, which
    is right for a nav bar and catastrophic for a product grid, a directory, an
    app store or a search-results page — where the link-dense block IS the
    content. The classifier already found the group; this asks whether it
    survived.
    """
    groups = [
        g for g in (getattr(cls, "repeated_groups", None) or []) if len(g) >= _LISTING_MIN_ITEMS
    ]
    if not groups:
        return False
    haystack = (markdown or "").casefold()
    if not haystack:
        return True
    # EVERY qualifying group, not just the dominant one. The scoring that picks
    # a winner rewards total text, so nine long testimonials outrank fifteen
    # short product cards — and it is the cards a caller came for. Guarding only
    # the winner guards the wrong block on exactly the pages that matter.
    for nodes in groups:
        kept = 0
        counted = 0
        for node in nodes:
            text = node_text(node)
            if not text:
                continue
            counted += 1
            # The first few words are enough to recognise the item, and survive
            # the whitespace and markup differences between DOM text and
            # markdown. But an item does not always OPEN with its content: a
            # forum post opens with an author and a visible date, and the
            # structured path renders the machine-readable date instead, so the
            # opening words match nothing while all five posts are present
            # (measured on the forum fixture, 7 Sep 2026). Ask about the item's
            # longest run of prose as well, and count it kept on either.
            if _recognises(text, haystack) or _recognises(_longest_run(node), haystack):
                kept += 1
        if counted and kept < counted * _LISTING_MIN_KEPT:
            return True
    return False


def _ensure_extractors_registered() -> None:
    if _REGISTRY:
        return
    # Imported here to avoid a circular import at module load.
    from engine.core.extract.heuristic import HeuristicExtractor
    from engine.core.extract.structured import StructuredExtractor

    register(HeuristicExtractor())
    register(StructuredExtractor())
