"""Layer 4 — content plausibility, and change tracking.

The plausibility tests carry an asymmetry worth stating: a false positive here
DISCARDS GENUINE CONTENT, which is worse than missing a decoy. So the
false-positive set is larger than the true-positive set, and the threshold
requires two independent signals rather than one.
"""

from __future__ import annotations

import pytest

from engine.core import change_tracking
from engine.core.detect.plausibility import (
    IMPLAUSIBLE_THRESHOLD,
    assess,
    sentence_length_variation,
    slug_overlap,
    type_token_ratio,
)

URL = "https://example.com/keto-diet-meal-prepping/"


def generated_filler(sentences: int = 30) -> str:
    """The shape of machine-generated decoy text: uniform length, narrow
    vocabulary, no incidental detail."""
    return " ".join(
        "The system provides comprehensive solutions for modern business needs."
        for _ in range(sentences)
    )


def genuine_prose() -> str:
    """Varied sentence length, specific detail, a date, an opinion."""
    return (
        "Keto meal prepping saves time. You batch-cook proteins on Sunday and "
        "portion them into glass containers. Here is why it actually matters: "
        "decision fatigue is the main reason diets fail by week three, and "
        "prepping removes the decision entirely. Published 12 March 2026. "
        "Some weeks I skip it. That is fine, because a rigid system nobody "
        "follows is worse than a loose one they do. The freezer is your friend "
        "for anything beyond four days, though texture suffers with egg dishes. "
    ) * 3


# --------------------------------------------------------------------------
# The decoy case this layer exists for
# --------------------------------------------------------------------------


def test_generated_filler_is_refused() -> None:
    """Cloudflare's AI Labyrinth serves plausible-looking generated pages to
    suspected crawlers, deliberately so naive scrapers ingest them."""
    signals = assess(url=URL, markdown=generated_filler(), page_type="article")
    assert signals.implausible
    assert len(signals.fired) >= IMPLAUSIBLE_THRESHOLD


def test_a_decoy_fires_several_independent_signals() -> None:
    signals = assess(url=URL, markdown=generated_filler(), page_type="article")
    assert {"lexical_diversity", "structural_monotony"} <= set(signals.fired)


def test_confidence_scales_with_agreement() -> None:
    """No single signal is conclusive; the score reflects how many agree."""
    signals = assess(url=URL, markdown=generated_filler(), page_type="article")
    assert 0.5 < signals.confidence <= 0.95


# --------------------------------------------------------------------------
# The false-positive guard — larger, because it matters more
# --------------------------------------------------------------------------


def test_genuine_prose_is_not_refused() -> None:
    signals = assess(
        url=URL,
        markdown=genuine_prose(),
        page_type="article",
        link_count=8,
        external_link_count=3,
        has_author=True,
    )
    assert not signals.implausible, f"genuine content refused: {signals.fired}"


def test_a_short_page_is_never_judged() -> None:
    """A short page is not a suspicious page. Below the analysis floor these
    signals mean nothing, and treating brevity as suspicion would discard a
    great deal of legitimate content."""
    signals = assess(url=URL, markdown="A brief but entirely real note.", page_type="article")
    assert not signals.analysed
    assert not signals.implausible


def test_technical_prose_with_repeated_terms_survives() -> None:
    """Documentation legitimately repeats its subject noun constantly, which
    depresses lexical diversity without being generated."""
    docs = (
        "The connection pool holds open connections. A connection is acquired "
        "from the pool, used, and returned to the pool. If the pool is empty "
        "the caller waits. Pool size is configured per process. Published "
        "3 April 2026 by the platform team, after a long argument about it. "
        "We settled on a small pool and better queue visibility instead. "
    ) * 4
    signals = assess(
        url="https://example.com/docs/connection-pool",
        markdown=docs,
        page_type="docs",
        external_link_count=2,
        has_author=True,
    )
    assert not signals.implausible, f"docs refused: {signals.fired}"


def test_a_page_whose_url_has_no_slug_is_not_penalised() -> None:
    """A bare domain gives nothing to compare against, which is not evidence
    of drift."""
    assert slug_overlap("https://example.com/", "any content at all") == 1.0


def test_a_numeric_url_is_not_penalised() -> None:
    """/posts/12345 carries no words to match, so topic drift cannot be
    assessed from it."""
    assert slug_overlap("https://example.com/posts/12345", "unrelated words") == 1.0


# --------------------------------------------------------------------------
# Individual signals
# --------------------------------------------------------------------------


def test_type_token_ratio_measures_vocabulary() -> None:
    assert type_token_ratio("a a a a a") < 0.3
    assert type_token_ratio("alpha beta gamma delta") == 1.0


def test_sentence_variation_detects_monotony() -> None:
    uniform = " ".join("one two three four five." for _ in range(10))
    varied = (
        "Short. This sentence is considerably longer than the one before it "
        "and carries more detail. Brief again. Then another long one that "
        "wanders somewhat before arriving at its point. Done."
    )
    assert sentence_length_variation(uniform) < sentence_length_variation(varied)


def test_topic_drift_detects_a_substituted_page() -> None:
    """Content bearing no relation to the path that led there is a
    substitution, not the article that was linked."""
    signals = assess(
        url="https://example.com/keto-diet-meal-prepping/",
        markdown=generated_filler(),
        page_type="article",
    )
    assert "topic_drift" in signals.fired


def test_a_product_without_a_price_is_suspicious() -> None:
    text = genuine_prose()
    signals = assess(url="https://example.com/shop/widget", markdown=text, page_type="product")
    assert "product_without_price" in signals.fired


def test_a_product_with_a_price_is_not() -> None:
    signals = assess(
        url="https://example.com/shop/widget",
        markdown=genuine_prose() + " Priced at £129.99 including delivery.",
        page_type="product",
        external_link_count=2,
        has_author=True,
    )
    assert "product_without_price" not in signals.fired


def test_measurements_are_recorded_even_when_nothing_fires() -> None:
    """Near-miss data is what lets the threshold be tuned later without
    re-crawling."""
    signals = assess(url=URL, markdown=genuine_prose(), page_type="article", external_link_count=2)
    assert "type_token_ratio" in signals.measurements
    assert "sentence_variation" in signals.measurements


# --------------------------------------------------------------------------
# Change tracking
# --------------------------------------------------------------------------


def test_volatile_content_is_normalised_away() -> None:
    """Without this a timestamp or a view counter makes every fetch look
    changed, and a change feed that cries wolf stops being read."""
    first = "Updated 14:32. Read by 1,204 views. The actual article body."
    second = "Updated 16:05. Read by 1,377 views. The actual article body."
    assert change_tracking.normalise(first) == change_tracking.normalise(second)


def test_a_real_edit_is_still_detected() -> None:
    first = "The pool holds ten connections."
    second = "The pool holds twenty connections."
    assert change_tracking.normalise(first) != change_tracking.normalise(second)


def test_relative_times_are_normalised() -> None:
    assert change_tracking.normalise("Posted 3 hours ago. Body.") == change_tracking.normalise(
        "Posted 9 hours ago. Body."
    )


def test_diff_shows_what_moved() -> None:
    diff = change_tracking.git_diff("line one\nline two", "line one\nline three")
    assert "-line two" in diff
    assert "+line three" in diff


def test_summary_counts_without_a_model_call() -> None:
    counts = change_tracking.summarise("a\nb\nc", "a\nb\nc\nd")
    assert counts["linesAdded"] == 1
    assert counts["linesRemoved"] == 0


@pytest.mark.parametrize(
    "status", [change_tracking.ChangeStatus.NEW, change_tracking.ChangeStatus.SAME]
)
def test_payload_shape_matches_the_contract(status: change_tracking.ChangeStatus) -> None:
    payload = change_tracking.ChangeResult(status=status).to_payload()
    assert payload["changeStatus"] == str(status)
    assert "previousScrapeAt" in payload
