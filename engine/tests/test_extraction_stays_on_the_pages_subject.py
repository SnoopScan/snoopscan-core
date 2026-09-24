"""When both extraction paths come up short, the one about the PAGE wins.

A software-review page reached through the deep tiers (Sep 2026) carried one
short review of its product and, below it, "not enough reviews — here are some
alternatives" with a long promotional description of a competitor. Both
extraction paths fell far below the page's visible text; the tie-break was word
count, and the competitor's blurb was the longest paragraph on the page, so the
caller would have been handed an advert for a different product.

Word count cannot tell an advert from the page's own content. Relevance can:
the page's heading names its subject, the review mentions it, the advert never
does. Consulted only when both paths have already failed the coverage check.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.core.extract import router
from engine.core.extract.router import ExtractionResult, ExtractOptions, extract, word_count

# The real shape, reduced: a heading naming the product, and enough visible
# text that both paths' results are far below the coverage floor.
CLEANED = (
    "<html><body><main><h1>Acme Widget Reviews &amp; Product Details</h1>"
    + "".join(
        f"<p>Visible page text number {i} that the extractors mostly missed.</p>" for i in range(80)
    )
    + "</main></body></html>"
)
ADVERT = " ".join(
    f"Globex Platform is a managed enterprise suite, sentence {i}, that helps "
    f"organisations build, train and deploy workflows faster."
    for i in range(12)
)
REVIEW = (
    "Great for small teams. What do you like best about Acme Widget? It syncs "
    "quickly. What do you dislike about Acme Widget? Exports are slow."
)


def _result(markdown: str) -> ExtractionResult:
    return ExtractionResult(markdown=markdown, word_count=word_count(markdown))


def _with_alternative(monkeypatch: pytest.MonkeyPatch, markdown: str) -> None:
    class _Alt:
        def extract(self, *args: Any, **kwargs: Any) -> ExtractionResult:
            return _result(markdown)

    router._ensure_extractors_registered()
    monkeypatch.setitem(router._REGISTRY, "structured", _Alt())
    monkeypatch.setitem(router._REGISTRY, "heuristic", _Alt())


def test_a_longer_result_about_something_else_loses(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.core.extract.classify import classify

    _with_alternative(monkeypatch, REVIEW)
    cls = classify(CLEANED)
    chosen = router._best_of(
        _result(ADVERT), CLEANED, "https://x.example/p", cls, router.ExtractOptions()
    )
    assert "Acme Widget" in chosen.markdown, "the page is about Acme Widget, not Globex"


def test_when_neither_names_the_subject_the_old_rule_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engine.core.extract.classify import classify

    _with_alternative(monkeypatch, "Short unrelated text about nothing in particular.")
    cls = classify(CLEANED)
    chosen = router._best_of(
        _result(ADVERT), CLEANED, "https://x.example/p", cls, router.ExtractOptions()
    )
    assert chosen.markdown == ADVERT


def test_a_page_whose_main_path_is_on_subject_is_left_alone() -> None:
    """The rule never overrides a result that already mentions the subject."""
    html = (
        "<html><head><title>Kettle guide</title></head><body><main><h1>Kettle guide</h1>"
        + "".join(
            f"<p>Kettle tip {i}: descale the kettle regularly for flavour.</p>" for i in range(40)
        )
        + "</main></body></html>"
    )
    ext = extract(html, "https://example.com/kettle", ExtractOptions())
    assert "Kettle tip 5" in ext.markdown


def test_relevance_is_the_last_word_when_the_listing_rescue_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real page counted as a listing (its competitor rows repeat), both
    paths 'lost' it, the whole-content rescue could not keep it either, and
    the fallback was the advert."""
    from engine.core.extract.classify import classify

    _with_alternative(monkeypatch, REVIEW)
    monkeypatch.setattr(router, "_dropped_the_listing", lambda *a, **k: True)
    monkeypatch.setattr(router, "_whole_main_content", lambda *a, **k: None)
    cls = classify(CLEANED)
    chosen = router._best_of(
        _result(ADVERT), CLEANED, "https://x.example/p", cls, router.ExtractOptions()
    )
    assert "Acme Widget" in chosen.markdown
