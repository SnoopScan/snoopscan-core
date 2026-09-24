"""A page that repeats itself per breakpoint is one page, not a forum.

Measured, Sep 2026, on a Framer marketing site:
Framer pages emit every section once per breakpoint, `aria-hidden` on all but
one. The duplicates were counted as repeating units, an "author" regex matched
"by the numbers", the page became a FORUM, the structured path stamped
`## Post 1`, and plausibility fired `forum_without_timestamps` plus
`topic_drift` (the slug `about-us` is not in the prose). A real 200 with 743
words was reported as a soft block. This file pins each link of that chain.
"""

from __future__ import annotations

from pathlib import Path

from selectolax.parser import HTMLParser

from engine.core.detect.plausibility import assess
from engine.core.detect.validator import ExtractionSummary, extract_title, validate
from engine.core.extract.classify import PageType, classify, find_repeated_blocks
from engine.core.extract.router import ExtractOptions, extract
from engine.core.fetch.base import FetchResult
from engine.core.fetch.escalation import DomainProfile

FIXTURE = (Path(__file__).parent / "fixtures" / "framer_about.html").read_text()
URL = "https://example.com/about-us"


def _section(text: str, n: int) -> str:
    prose = "Real prose about the thing. " * 12
    return f"<section><h2>{text}</h2><p>{prose} Version {n}.</p></section>"


def test_identical_sibling_blocks_are_duplication_not_repetition() -> None:
    copies = "".join(_section("What We Stand For", 1) for _ in range(3))
    _, count, _ = find_repeated_blocks(HTMLParser(f"<html><body>{copies}</body></html>"))
    assert count < 3, "three copies of one section are one section"

    cards = "".join(_section(f"Card {i}", i) for i in range(3))
    _, count, _ = find_repeated_blocks(HTMLParser(f"<html><body>{cards}</body></html>"))
    assert count == 3, "three different cards are still a repeating unit"


def test_the_framer_about_page_is_not_a_forum() -> None:
    cls = classify(FIXTURE)
    assert cls.page_type != PageType.FORUM, f"classified as {cls.page_type}"


def test_extraction_does_not_stamp_synthetic_post_headings() -> None:
    ext = extract(FIXTURE, URL, ExtractOptions())
    assert "## Post " not in ext.markdown
    # The page reads as ~740 words only because every section is rendered two
    # or three times; its UNIQUE copy is ~320 words, and that is what a caller
    # should receive — once.
    assert ext.word_count > 250, ext.word_count
    headings = [line for line in ext.markdown.splitlines() if line.startswith("## ")]
    assert len(headings) == len(set(headings)), f"duplicated headings survived: {headings}"


def test_topic_drift_does_not_fire_on_a_utility_slug() -> None:
    prose = "We believe in doing the work, not chasing the spotlight. " * 40
    about = assess(url="https://example.com/about-us", markdown=prose, page_type="unknown")
    assert "topic_drift" not in about.fired
    home = assess(url="https://example.com/", markdown=prose, page_type="unknown")
    assert "topic_drift" not in home.fired
    # A content slug that promised a subject the body never mentions still drifts.
    article = assess(
        url="https://example.com/blog/quantum-widgets", markdown=prose, page_type="unknown"
    )
    assert "topic_drift" in article.fired


def test_the_real_page_validates_as_content_end_to_end() -> None:
    body = FIXTURE.encode()
    result = FetchResult(
        url=URL,
        status_code=200,
        headers={"content-type": "text/html"},
        body=body,
        content_type="text/html",
        tier="http",
        latency_ms=1,
        bytes_transferred=len(body),
    )
    ext = extract(result.text(), URL, ExtractOptions())
    summary = ExtractionSummary(
        markdown=ext.markdown,
        page_type=str(ext.page_type),
        word_count=ext.word_count,
        char_count=ext.char_count,
        confidence=ext.confidence,
        link_count=len(ext.links),
        title=ext.title or extract_title(result.text(limit=20_000)),
    )
    verdict = validate(result, DomainProfile("example.com").to_stats(), summary)
    assert verdict.ok, f"{verdict.signal}: {dict(verdict.details) if verdict.details else {}}"
