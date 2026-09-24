"""Heuristic extraction path — articles, docs, unknown (04-extraction.md s3).

trafilatura >= 1.8.0 (Apache-2.0 from that version; earlier releases are
GPLv3+ and forbidden by constraint C2 — the licence gate asserts the floor).

Includes the mandated fallback chain: precision, then recall, then a
readability-style pass. Which one produced the output is recorded, because
"extraction quietly degraded" is otherwise invisible.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser, Node

from engine.core.extract import code_blocks
from engine.core.extract.classify import Classification
from engine.core.extract.router import (
    CHARS_PER_WORD,
    COVERAGE_FLOOR,
    ROUTES,
    ExtractionResult,
    ExtractOptions,
    collect_links,
    raw_text_length,
    word_count,
)
from engine.core.extract.text import node_text
from engine.core.models import ExtractionPath

# Below this, trafilatura has failed on a page that clearly has content.
MIN_ACCEPTABLE_WORDS = 50
SUBSTANTIAL_RAW_CHARS = 500
# ...and below this share of the page's own text, it has failed too, however
# many words it found. An absolute floor let a 70-word block stand in for a
# 740-word Framer page (measured: 514 of 9,342 chars, deterministically).
# The share itself lives in the router, because the SAME number decides whether
# to try the other extractor; two copies would drift.
_CHARS_PER_WORD = CHARS_PER_WORD


ORPHANS_TO_RETRY = 3


def _words(text: str) -> str:
    """Words only: bullets, dashes and line breaks differ between page and output."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _text_after(heading: Node, limit: int = 60) -> str:
    """The words that follow a heading inside its entry, up to `limit` chars."""
    parts: list[str] = []
    sibling = heading.next
    while sibling is not None and len(" ".join(parts)) < limit:
        if sibling.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            break  # the next entry starts here
        text = node_text(sibling) if sibling.tag != "-text" else (sibling.text() or "")
        if _words(text):
            parts.append(_words(text))
        sibling = sibling.next
    return " ".join(parts)[:limit].rsplit(" ", 1)[0]


def orphaned_entries(html: str, markdown: str) -> int:
    """Entries whose linked heading is missing from the output but whose
    details are present: a name dropped from a listing that kept the rest.

    Companies House's search came back with every company's number and address
    and no company names (22 Sep 2026). A sidebar of related links is not
    counted, because its blurbs are dropped along with its headings.
    """
    if not markdown:
        return 0
    out = _words(markdown)
    orphans = 0
    for heading in HTMLParser(html).css("h2, h3, h4"):
        if heading.css_first("a") is None:
            continue
        name = _words(node_text(heading))
        if not name or name in out:
            continue
        details = _text_after(heading)
        if len(details) >= 20 and details in out:
            orphans += 1
    return orphans


def under_extracted(words: int, raw_len: int) -> bool:
    """Has the precision pass kept too little of a page that clearly has more?"""
    if raw_len <= SUBSTANTIAL_RAW_CHARS:
        return False
    if words < MIN_ACCEPTABLE_WORDS:
        return True
    return words < COVERAGE_FLOOR * (raw_len // _CHARS_PER_WORD)


def is_richer_extraction(candidate: str | None, incumbent: str | None) -> bool:
    """More words only wins if the words are a page.

    Every rung of this chain chose on word count alone, so on IMDb's homepage
    a recall pass that returned 324 words of nav and language picker beat the
    167 words of real featured content that came before it. More words, no
    page — and the caller was billed 5 credits for a menu. Measured 9 Sep
    2026: 3 of 17 captures took a fallback that was strictly worse.
    """
    from engine.core.detect.validator import is_nav_shell

    if not candidate:
        return False
    words = word_count(candidate)
    if words <= word_count(incumbent or ""):
        return False

    # Chrome never wins, however much of it there is.
    return not is_nav_shell(candidate, words)


class HeuristicExtractor:
    name = "heuristic"

    def extract(
        self,
        html: str,
        url: str,
        cls: Classification,
        options: ExtractOptions,
    ) -> ExtractionResult:

        favour_precision = options.only_main_content

        markdown = self._run(html, url, options, favour_precision=favour_precision)
        path = ExtractionPath.HEURISTIC

        # Fallback chain: a page whose raw text exceeds 500 chars but yields
        # under 50 words has not been extracted, it has been lost.
        raw_len = raw_text_length(html)
        if under_extracted(word_count(markdown or ""), raw_len):
            retry = self._run(html, url, options, favour_precision=False)
            if is_richer_extraction(retry, markdown):
                markdown, path = retry, ExtractionPath.FALLBACK

        # A listing whose entry NAMES went missing while their details stayed:
        # trafilatura's precision mode reads a heading that is all link as
        # navigation. On a register that heading is the business. Not on a page
        # routed as a listing: the structured path owns those and pairs each
        # name with its link and blurb, which this retry cannot.
        if (
            favour_precision
            and path == ExtractionPath.HEURISTIC
            and ROUTES.get(cls.page_type) != "structured"
            and orphaned_entries(html, markdown or "") >= ORPHANS_TO_RETRY
        ):
            retry = self._run(html, url, options, favour_precision=False)
            if retry and orphaned_entries(html, retry) < orphaned_entries(html, markdown or ""):
                markdown, path = retry, ExtractionPath.FALLBACK

        if under_extracted(word_count(markdown or ""), raw_len):
            readability = self._readability_fallback(html)
            if is_richer_extraction(readability, markdown):
                markdown, path = readability, ExtractionPath.FALLBACK

        # trafilatura decides prose well and handles code badly: it flattens
        # single-line blocks to inline code and drops language hints. Repair
        # from the source rather than replacing an extractor that is otherwise
        # doing its job.
        markdown = code_blocks.restore(markdown or "", html)

        metadata = self._metadata(html, url)

        return ExtractionResult(
            markdown=markdown or "",
            html=html,
            links=collect_links(html, url) if options.include_links else [],
            title=metadata.get("title"),
            description=metadata.get("description"),
            language=metadata.get("language"),
            author=metadata.get("author"),
            published_at=metadata.get("published_at"),
            extraction_path=path,
        )

    def _run(
        self, html: str, url: str, options: ExtractOptions, *, favour_precision: bool
    ) -> str | None:
        import trafilatura

        try:
            return trafilatura.extract(
                html,
                url=url,
                output_format="markdown",
                include_links=options.include_links,
                include_tables=True,
                include_images=False,
                include_comments=False,
                favor_precision=favour_precision,
                favor_recall=not favour_precision,
                with_metadata=False,
            )
        except (ValueError, TypeError, AttributeError):
            # trafilatura raises on some malformed documents. A failed
            # extraction is a None here and the fallback chain handles it.
            return None

    def _metadata(self, html: str, url: str) -> dict[str, str | None]:
        """trafilatura's metadata extraction is good — use it rather than
        writing our own, but cross-check the title against <title> and
        Open Graph and prefer the longest sensible value."""
        import trafilatura

        out: dict[str, str | None] = {}
        try:
            meta = trafilatura.extract_metadata(html, default_url=url)
        except (ValueError, TypeError, AttributeError):
            meta = None

        if meta is not None:
            out["title"] = meta.title or None
            out["author"] = meta.author or None
            out["description"] = meta.description or None
            out["published_at"] = meta.date or None
            # trafilatura's language detection is optional at runtime.
            out["language"] = getattr(meta, "language", None) or None
        return out

    def _readability_fallback(self, html: str) -> str:
        """Last resort: density-based main-content selection.

        Picks the block element with the best text-to-markup ratio and emits
        its paragraphs. Crude by design — it only runs when everything else
        returned nothing.
        """
        tree = HTMLParser(html)
        best_node = None
        best_score = 0.0

        for node in tree.css("article, main, div, section"):
            text = node_text(node)
            if len(text) < 200:
                continue
            markup_len = len(node.html or "") or 1
            paragraphs = len(node.css("p"))
            density = len(text) / markup_len
            node_score = len(text) * density * (1 + paragraphs * 0.1)
            if node_score > best_score:
                best_score, best_node = node_score, node

        if best_node is None:
            body = tree.body or tree.root
            return node_text(body)

        parts: list[str] = []
        for child in best_node.css("h1, h2, h3, h4, p, li, blockquote, pre"):
            text = node_text(child)
            if not text:
                continue
            tag = child.tag or "p"
            if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
                parts.append(f"{'#' * int(tag[1])} {text}")
            elif tag == "li":
                parts.append(f"- {text}")
            elif tag == "blockquote":
                parts.append(f"> {text}")
            elif tag == "pre":
                parts.append(f"```\n{text}\n```")
            else:
                parts.append(text)
        return "\n\n".join(parts)
