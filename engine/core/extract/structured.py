"""Structured extraction path — forums, products, listings, tables.

04-extraction.md section 4. This is where the engine beats heuristic
extractors: they assume one article surrounded by boilerplate, which is true
for news and false everywhere else. On forums that assumption costs roughly
half the achievable F1.

Two mechanisms:
  4a. structure-preserving conversion — tables stay tables, code stays fenced,
      list nesting survives, forum quotes stay nested blockquotes
  4b. repeated-block detection — find the repeating unit (a post, a product
      card, a listing row) and extract each instance separately, instead of
      guessing at "main content" that does not exist on these pages
"""

from __future__ import annotations

import re
from typing import Any

from selectolax.parser import HTMLParser, Node

from engine.core.extract.classify import Classification, structural_signature
from engine.core.extract.router import (
    ExtractionResult,
    ExtractOptions,
    collect_links,
)
from engine.core.extract.text import node_text
from engine.core.models import ExtractionPath, PageType

_TIMESTAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)"
    r"|(\d{1,2}\s+\w+\s+\d{4})"
    r"|(\d+\s+(?:minute|hour|day|week|month|year)s?\s+ago)",
    re.IGNORECASE,
)
_AUTHOR_ATTR_SELECTORS = (
    '[itemprop="author"]',
    '[class*="author"]',
    '[class*="username"]',
    '[class*="user-name"]',
    '[rel="author"]',
    ".byline",
)
_TIME_SELECTORS = ("time", "[datetime]", '[class*="timestamp"]', '[class*="date"]')

# A colspan is expanded across the columns it covers. Capped, because the
# attribute is caller-controlled and a colspan of 9999 would be a memory bomb.
_MAX_COLSPAN = 20


class StructuredExtractor:
    name = "structured"

    def extract(
        self,
        html: str,
        url: str,
        cls: Classification,
        options: ExtractOptions,
    ) -> ExtractionResult:
        tree = HTMLParser(html)

        if cls.page_type == PageType.FORUM:
            markdown, structured = self._extract_forum(tree, cls)
        elif cls.page_type == PageType.LISTING:
            markdown, structured = self._extract_listing(tree, cls, url)
        elif cls.page_type == PageType.PRODUCT:
            markdown, structured = self._extract_product(tree, cls)
        else:  # TABLE-dominant
            markdown, structured = self._extract_tables(tree)

        # Nothing recognisable — fall back rather than returning an empty body.
        if not markdown.strip():
            markdown = convert_to_markdown(main_content_node(tree))
            path = ExtractionPath.FALLBACK
        else:
            path = ExtractionPath.STRUCTURED

        return ExtractionResult(
            markdown=markdown,
            html=html,
            links=collect_links(html, url) if options.include_links else [],
            structured=structured,
            extraction_path=path,
        )

    # -- forums ------------------------------------------------------------

    def _extract_forum(self, tree: HTMLParser, cls: Classification) -> tuple[str, dict[str, Any]]:
        """One section per post, author and timestamp as a heading, quote
        nesting preserved as blockquotes."""
        nodes = _nodes_for_signature(tree, cls.repeated_selector)
        if not nodes:
            return "", {}

        posts: list[dict[str, Any]] = []
        parts: list[str] = []
        for index, node in enumerate(nodes, start=1):
            author = _first_text(node, _AUTHOR_ATTR_SELECTORS)
            timestamp = _first_timestamp(node)
            body = convert_to_markdown(
                node, skip_selectors=_AUTHOR_ATTR_SELECTORS + _TIME_SELECTORS
            )
            if not body.strip():
                continue

            heading_bits = [author or f"Post {index}"]
            if timestamp:
                heading_bits.append(timestamp)
            parts.append(f"## {' — '.join(heading_bits)}\n\n{body.strip()}")
            posts.append({"index": index, "author": author, "timestamp": timestamp, "body": body})

        return "\n\n".join(parts), {"type": "forum", "posts": posts, "count": len(posts)}

    # -- listings ----------------------------------------------------------

    def _extract_listing(
        self, tree: HTMLParser, cls: Classification, url: str
    ) -> tuple[str, dict[str, Any]]:
        """Each card becomes an entry with title, link and description."""
        nodes = _nodes_for_signature(tree, cls.repeated_selector)
        if not nodes:
            return "", {}

        items: list[dict[str, Any]] = []
        parts: list[str] = []
        for node in nodes:
            link_node = node.css_first("a[href]")
            href = (link_node.attributes.get("href") or "").strip() if link_node else None
            title = _first_text(node, ("h1", "h2", "h3", "h4", "h5", '[class*="title"]'))
            if not title and link_node:
                title = node_text(link_node) or None
            description = _description_for(node, title)

            if not title and not description:
                continue

            items.append({"title": title, "url": href, "description": description})
            label = title or "(untitled)"
            line = f"- **{label}**" if not href else f"- **[{label}]({href})**"
            if description:
                line += f"\n  {description}"
            parts.append(line)

        return "\n".join(parts), {"type": "listing", "items": items, "count": len(items)}

    # -- products ----------------------------------------------------------

    def _extract_product(self, tree: HTMLParser, cls: Classification) -> tuple[str, dict[str, Any]]:
        """Structured markup first — it is free and usually answers directly."""
        fields: dict[str, Any] = {}
        for block in cls.structured_data:
            types = block.get("@type")
            types = [types] if isinstance(types, str) else (types or [])
            if not any(str(t).lower() in {"product", "offer"} for t in types):
                continue
            for key in ("name", "description", "sku", "brand", "gtin", "mpn"):
                value = block.get(key)
                if value is not None and key not in fields:
                    fields[key] = _flatten_ld(value)
            offers = block.get("offers")
            offer_list = offers if isinstance(offers, list) else [offers] if offers else []
            for offer in offer_list:
                if not isinstance(offer, dict):
                    continue
                for src, dest in (
                    ("price", "price"),
                    ("priceCurrency", "currency"),
                    ("availability", "availability"),
                ):
                    if offer.get(src) is not None and dest not in fields:
                        fields[dest] = humanise_schema_enum(_flatten_ld(offer[src]))

        body = convert_to_markdown(main_content_node(tree))
        parts: list[str] = []
        if fields:
            parts.append("## Product details\n")
            for key, value in fields.items():
                parts.append(f"- **{key}**: {value}")
            parts.append("")
        if body.strip():
            parts.append(body.strip())

        return "\n".join(parts), {"type": "product", "fields": fields} if fields else {}

    # -- tables ------------------------------------------------------------

    def _extract_tables(self, tree: HTMLParser) -> tuple[str, dict[str, Any]]:
        tables = tree.css("table")
        if not tables:
            return "", {}
        parts = [table_to_markdown(table) for table in tables]
        parts = [p for p in parts if p.strip()]
        return "\n\n".join(parts), {"type": "table", "count": len(parts)}


# --------------------------------------------------------------------------
# Structure-preserving conversion (4a)
# --------------------------------------------------------------------------


# A table used for LAYOUT carries no tabular data, so preserving its structure
# preserves nothing. behindthename.com builds its menu bar as
# `<table id="menubar-table">` with nested tables — which `table_to_markdown`
# read as "too complex for markdown" and embedded as raw HTML, making the
# site's navigation the page's entire content, byte-identical for every name
# (measured on /name/aspen, /name/juniper, /name/willow, /name/ivy,
# 7 Sep 2026: 3,850 characters of menu bar where the page had 18-36 KB).
_NAV_TABLE_LINK_RATIO = 0.6


def _is_navigation_table(table: Node) -> bool:
    """Is this table page furniture rather than data?

    Three things together, because any one alone has honest counter-examples:
    no header cells (a data table almost always has them), text that is
    overwhelmingly link anchors, and no sentences at all. The menu bar scores
    no <th>, 88% link text and zero full stops.
    """
    if table.css("th"):
        return False
    text = node_text(table)
    if not text:
        return True
    link_chars = sum(len(node_text(a)) for a in table.css("a"))
    if link_chars / max(len(text), 1) < _NAV_TABLE_LINK_RATIO:
        return False
    return "." not in text


def table_to_markdown(table: Node) -> str:
    """Markdown table, preserving the header row and expanding merged cells.

    Merged and nested cells used to be emitted as a raw HTML block, on the
    reasoning that destroying the structure is worse than embedding it. Two
    real pages showed that trade is the wrong way round: hetzner.com's product
    matrix came back as 71 KB of `<table>` in the `markdown` field, and because
    tags count as words it BEAT the readable extraction and was handed to the
    caller (7 Sep 2026). Raw HTML in a markdown field is not a preserved
    structure — it is unusable by the caller AND by every word match downstream.

    A colspan is expanded across the columns it covers, which is lossy about the
    merge and faithful about the CONTENT. That is the right way round: the
    reader wanted the specifications, not the layout.
    """
    # Furniture first: a nav table embedded as raw HTML is how a menu bar
    # becomes a page's content.
    if _is_navigation_table(table):
        return ""

    rows = table.css("tr")
    if not rows:
        # Nothing parseable. Only here is the raw block still better than
        # silence, because there are no rows to render.
        html = table.html or ""
        return f"\n{html}\n" if html.strip() else ""

    parsed: list[list[str]] = []
    for row in rows:
        cells = row.css("th, td")
        if not cells:
            continue
        out_row: list[str] = []
        for cell in cells:
            text = re.sub(r"\s+", " ", node_text(cell)).replace("|", "\\|")
            try:
                span = int(cell.attributes.get("colspan") or 1)
            except (TypeError, ValueError):
                span = 1
            out_row.extend([text] * max(1, min(span, _MAX_COLSPAN)))
        parsed.append(out_row)
    if not parsed:
        return ""

    width = max(len(r) for r in parsed)
    parsed = [r + [""] * (width - len(r)) for r in parsed]

    has_header = bool(rows[0].css_first("th"))
    header = parsed[0] if has_header else [""] * width
    body = parsed[1:] if has_header else parsed

    lines = ["| " + " | ".join(header) + " |", "|" + "|".join([" --- "] * width) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def convert_to_markdown(node: Node | None, skip_selectors: tuple[str, ...] = ()) -> str:
    """Convert a subtree to markdown with structure intact.

    Uses html-to-markdown (MIT) as the base rather than a hand-rolled
    converter — HTML edge cases are endless — with our own table handling
    layered on for the complex-table case.
    """
    if node is None:
        return ""

    html = node.html or ""
    if not html.strip():
        return ""

    working = HTMLParser(html)
    for selector in skip_selectors:
        for match in working.css(selector):
            match.decompose()

    # Tables are swapped for placeholder paragraphs so our own converter owns
    # them (the general converter flattens complex tables), then substituted
    # back after conversion.
    placeholders: dict[str, str] = {}
    for index, table in enumerate(working.css("table")):
        token = f"XTABLEPLACEHOLDERX{index}X"
        placeholders[token] = table_to_markdown(table)
        # The bare token: replace_with() inserts TEXT, so "<p>token</p>" came
        # out as a literal "<p>" either side of every table (indeed.com, 22 Sep
        # 2026). The substitution below adds the line breaks a table needs.
        table.replace_with(token)

    source = working.html or html

    markdown = _library_convert(source)
    if markdown is None:
        markdown = _fallback_convert(HTMLParser(source))

    for token, table_md in placeholders.items():
        markdown = markdown.replace(token, f"\n{table_md}\n")

    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip()


def _library_convert(source: str) -> str | None:
    """Convert via html-to-markdown (MIT), or None if it is unusable.

    Returning None rather than raising lets the caller fall back. The import
    is guarded because a library API change must degrade visibly to the
    fallback converter rather than taking the whole extraction down — and the
    mypy/type check plus the fixture suite are what catch the degradation.
    """
    try:
        from html_to_markdown import ConversionOptions, convert
    except ImportError:
        return None

    options = ConversionOptions(
        heading_style="atx",
        bullets="-",
        escape_asterisks=False,
        escape_underscores=False,
        escape_misc=False,
        code_block_style="backticks",
    )
    try:
        result = convert(source, options)
    except Exception:  # noqa: BLE001 - any conversion failure falls back
        return None

    # v3 returns a ConversionResult (markdown on `.content`); v2 returned a
    # plain string. Handle both so a version bump degrades loudly in tests
    # rather than silently emptying every structured extraction.
    if isinstance(result, str):
        return result
    markdown = getattr(result, "content", None)
    return markdown if isinstance(markdown, str) else None


def _fallback_convert(tree: HTMLParser) -> str:
    """Minimal structure-preserving conversion, used only when the library
    conversion raises."""
    parts: list[str] = []
    body = tree.body or tree.root
    if body is None:
        return ""
    for node in body.css("h1, h2, h3, h4, h5, h6, p, li, blockquote, pre, table"):
        tag = node.tag or "p"
        if tag == "table":
            parts.append(table_to_markdown(node))
            continue
        text = node_text(node)
        if not text:
            continue
        if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            parts.append(f"{'#' * int(tag[1])} {text}")
        elif tag == "li":
            parts.append(f"- {text}")
        elif tag == "blockquote":
            parts.append("\n".join(f"> {line}" for line in text.split("\n")))
        elif tag == "pre":
            parts.append(f"```\n{text}\n```")
        else:
            parts.append(text)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _nodes_for_signature(tree: HTMLParser, signature: str | None) -> list[Node]:
    if not signature:
        return []
    out: list[Node] = []
    for node in tree.css("div, li, article, section, tr"):
        text = node.text(deep=True, strip=True) or ""
        if len(text) < 40:
            continue
        if structural_signature(node) == signature:
            out.append(node)
    return out


def _first_text(node: Node, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        found = node.css_first(selector)
        if found:
            text = node_text(found)
            if text and len(text) < 120:
                return re.sub(r"\s+", " ", text)
    return None


def _first_timestamp(node: Node) -> str | None:
    for selector in _TIME_SELECTORS:
        found = node.css_first(selector)
        if not found:
            continue
        attr = found.attributes.get("datetime")
        if attr:
            return attr.strip()
        text = node_text(found)
        if text and _TIMESTAMP.search(text):
            return re.sub(r"\s+", " ", text)[:60]
    match = _TIMESTAMP.search(node_text(node))
    return match.group(0) if match else None


def _description_for(node: Node, title: str | None) -> str | None:
    for candidate in node.css("p, [class*='desc'], [class*='excerpt'], [class*='summary']"):
        text = node_text(candidate)
        if text and text != title and len(text) > 20:
            return re.sub(r"\s+", " ", text)[:300]
    text = node_text(node)
    if title and text.startswith(title):
        text = text[len(title) :]
    text = re.sub(r"\s+", " ", text).strip()
    return text[:300] or None


# schema.org expresses enums as URLs. Emitting the raw URL is not output a
# human or a model wants, and it pollutes every downstream consumer.
_SCHEMA_ENUM = re.compile(r"^https?://schema\.org/(\w+)$", re.IGNORECASE)
_ENUM_WORDS = {
    "instock": "In stock",
    "outofstock": "Out of stock",
    "preorder": "Pre-order",
    "backorder": "On back-order",
    "discontinued": "Discontinued",
    "limitedavailability": "Limited availability",
    "new": "New",
    "usedcondition": "Used",
    "refurbishedcondition": "Refurbished",
}


def humanise_schema_enum(value: Any) -> Any:
    """Turn https://schema.org/InStock into "In stock"."""
    if not isinstance(value, str):
        return value
    match = _SCHEMA_ENUM.match(value.strip())
    if not match:
        return value
    token = match.group(1)
    return _ENUM_WORDS.get(token.lower(), re.sub(r"(?<!^)(?=[A-Z])", " ", token))


def _flatten_ld(value: Any) -> Any:
    """JSON-LD values are often nested objects or single-element lists."""
    if isinstance(value, dict):
        return value.get("name") or value.get("@id") or str(value)
    if isinstance(value, list):
        return ", ".join(str(_flatten_ld(v)) for v in value)
    return value


# Page furniture. Stripped before falling back to <body>, because the fallback
# is precisely the case where nothing marked the content out and the nav and
# footer would otherwise be treated as part of it.
_CHROME_SELECTORS = (
    "nav",
    "header",
    "footer",
    "aside",
    '[role="navigation"]',
    '[role="banner"]',
    '[role="contentinfo"]',
    ".cc",
    "[class*='cookie']",
    "[class*='consent']",
)

# A short <main> is still the main content. The old floor of 200 characters
# rejected a genuine product page and fell through to <body>, which dragged
# the footer into the output.
_MIN_MAIN_CHARS = 80


def main_content_node(tree: HTMLParser) -> Node | None:
    """The element holding the page's content.

    Prefers an explicit container; when none qualifies, falls back to the body
    with page furniture removed rather than to the body whole.
    """
    for selector in ("article", "main", '[role="main"]', "#content", ".content"):
        node = tree.css_first(selector)
        if node and len(node.text(deep=True, strip=True) or "") > _MIN_MAIN_CHARS:
            return node

    body = tree.body or tree.root
    if body is None:
        return None
    for selector in _CHROME_SELECTORS:
        for chrome in body.css(selector):
            chrome.decompose()
    return body
