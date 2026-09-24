"""Routing and cross-check regressions found by the Phase 1 checkpoint.

Both bugs here presented as "the fetch failed" — a page returning almost no
text looks identical whether the content was never sent, was blocked, or was
sent and then thrown away by the wrong extractor. Measuring the live target
set is what separated them.
"""

from __future__ import annotations

from engine.core.extract.classify import classify
from engine.core.extract.router import extract
from engine.core.models import PageType


def paragraphs(count: int, topic: str = "the subject") -> str:
    """Varied prose. Identical repeated lines are collapsed by the boilerplate
    sweep — correctly, since that is a failed-extraction artefact — so a
    fixture built from one repeated sentence measures the sweep, not the
    extractor."""
    return "".join(
        f"<p>Paragraph {i} discusses {topic} in specific terms, with enough "
        f"distinct wording that it reads as genuine editorial prose rather "
        f"than filler duplicated across the page body.</p>"
        for i in range(count)
    )


def news_index(article_count: int) -> str:
    """A news homepage: every teaser card marked up as <article>."""
    cards = "".join(
        f"<article><h2>Story number {i}</h2>"
        f"<p>A teaser paragraph summarising story {i} in enough words to be "
        f"treated as genuine content rather than a fragment.</p>"
        f"<a href='/story-{i}'>Read more</a></article>"
        for i in range(article_count)
    )
    return (
        "<html lang='en'><head><title>Tech News</title>"
        "<meta property='og:type' content='article'></head>"
        f"<body><nav><a href='/'>Home</a></nav>{cards}</body></html>"
    )


def single_article() -> str:
    return (
        "<html lang='en'><head><title>One Story</title>"
        "<meta property='og:type' content='article'></head><body>"
        f"<article><h1>One Story</h1>{paragraphs(8, 'the reported story')}"
        "</article></body></html>"
    )


# --------------------------------------------------------------------------
# One <article> is an article; many is an index of articles
# --------------------------------------------------------------------------


def test_a_single_article_element_is_an_article() -> None:
    assert classify(single_article()).page_type == PageType.ARTICLE


def test_many_article_elements_are_a_listing() -> None:
    """Measured live: a news homepage with 46 <article> elements was scored as
    a single article, routed to the single-article extractor, and yielded 355
    characters out of 125,317 available. The COUNT is the signal."""
    assert classify(news_index(20)).page_type == PageType.LISTING


def test_og_type_article_does_not_override_a_page_full_of_articles() -> None:
    """og:type describes one article; on an index it is template metadata
    copied across every page, so it needs corroboration."""
    html = news_index(30)
    assert "og:type" in html
    assert classify(html).page_type == PageType.LISTING


def test_a_news_index_extracts_far_more_than_one_teaser() -> None:
    result = extract(news_index(20), "https://news.example.com/")
    assert result.word_count > 200, (
        f"only {result.word_count} words from a 20-story index — the page was "
        f"routed to the wrong extractor"
    )


# --------------------------------------------------------------------------
# Cross-check: whichever path wins, it must not lose the page
# --------------------------------------------------------------------------


def test_extraction_falls_back_when_the_routed_path_loses_the_page() -> None:
    """A correct classification is no comfort if it produces a worse result.

    Measured live: a directory homepage classified (correctly) as `listing`
    yielded 86 words where the heuristic path got 606.
    """
    # Prose-heavy body that the structured path handles poorly but reads as a
    # listing structurally.
    cards = "".join(f"<div class='row'><a href='/x{i}'>Item {i}</a></div>" for i in range(12))
    prose = paragraphs(30, "the directory listing")
    html = (
        f"<html lang='en'><head><title>Directory</title></head><body>"
        f"<nav>{cards}</nav><main>{prose}</main></body></html>"
    )
    result = extract(html, "https://directory.example.com/")
    assert result.word_count > 300, (
        f"only {result.word_count} words survived; the cross-check did not rescue the page"
    )


def test_cross_check_leaves_a_good_extraction_alone() -> None:
    """It must only run when the primary looks bad — otherwise every page
    pays for two extractions."""
    result = extract(single_article(), "https://example.com/story")
    assert result.page_type == PageType.ARTICLE
    assert result.word_count > 100


def test_short_pages_are_not_second_guessed() -> None:
    """Below the raw-text floor the ratio means nothing, so the cross-check
    must not fire on a legitimately brief page."""
    html = (
        "<html lang='en'><head><title>Note</title></head><body><article>"
        "<h1>Short note</h1><p>Brief but real.</p></article></body></html>"
    )
    result = extract(html, "https://example.com/note")
    assert result.markdown.strip()
