"""A page whose content IS a grid of links must not come back as prose.

Readability-style extraction treats link-dense blocks as navigation. That is
right for a nav bar and catastrophic for a product grid, a directory, an app
store or a search-results page, where the link-dense block is the entire point
of the page.

Measured on a live integrations directory (Sep 2026): fifteen cards, each a
short `<a>` with a title and one line of description, every one of them present
in the HTML and none of them in our markdown. The caller got 4,481 characters of
testimonials and footer, `confidence: 1.0`, and no indication anything was
missing — which is the worst possible failure for a paid scrape, because it
looks exactly like a success.

The word-count coverage floor could not catch it: fifteen cards were 13% of the
page's words, so coverage read 84%. The check has to be structural — did the
repeating unit the page is built from survive?
"""

from __future__ import annotations

import pytest

from engine.core.extract.classify import classify
from engine.core.extract.router import ExtractOptions, extract, preclean

CARDS = "".join(
    f'<a href="/tool/{i}" class="card"><div class="t">Tool Number {i}</div>'
    f'<div class="d">Scrape the web from Tool {i}, without leaving your editor.</div></a>'
    for i in range(1, 16)
)

# Enough prose that a readability extractor happily returns it and stops.
# VARIED prose, deliberately: identical repeated sentences are collapsed as
# duplication before anything else can be measured, which makes a fixture prove
# the wrong thing.
_LINES = [
    "We replaced a fortnight of brittle selector work with one call.",
    "The success rate on protected retail sites is what sold our team.",
    "Their support answered a schema question inside the hour, twice.",
    "We pull competitor pricing every morning and nothing has broken.",
    "Onboarding took an afternoon; the docs were accurate throughout.",
    "It handles the awkward JavaScript pages our old stack gave up on.",
    "Costs dropped by a third once we stopped renting our own proxies.",
    "The change feed means we stop re-reading pages that never moved.",
    "Clean markdown straight into the vector store, no massaging needed.",
]
PROSE = "".join(
    f"<div class='quote'><p>{line} {line} It has held up under real load.</p>"
    f"<span>Reviewer {i}, Head of Data</span></div>"
    for i, line in enumerate(_LINES, start=1)
)

DIRECTORY = f"""<!doctype html><html><head><title>Integrations</title></head><body>
<header><nav><a href="/">Home</a><a href="/pricing">Pricing</a></nav></header>
<main>
  <h1>Integrations</h1>
  <p>Plug the API into the tools you already build with.</p>
  <div class="grid">{CARDS}</div>
  <section class="testimonials">{PROSE}</section>
</main>
<footer><a href="/legal">Legal</a></footer></body></html>"""


def _markdown() -> str:
    return extract(DIRECTORY, "https://example.com/integrations/", ExtractOptions()).markdown or ""


def test_the_card_grid_survives_extraction() -> None:
    md = _markdown()
    kept = sum(1 for i in range(1, 16) if f"Tool Number {i}" in md)
    assert kept >= 12, f"only {kept}/15 cards survived; the listing was dropped"


def test_the_prose_on_the_same_page_also_survives() -> None:
    """Rescuing the listing must not swap one loss for another."""
    assert "Reviewer 1" in _markdown()


def test_the_classifier_finds_the_grid_as_a_repeated_group() -> None:
    """The nodes are what the structural check reads; without them it is inert."""
    cls = classify(preclean(DIRECTORY, ExtractOptions()))
    assert len(cls.repeated_nodes) >= 5
    assert cls.repeated_count == len(cls.repeated_nodes)


def test_the_check_reports_a_dropped_listing() -> None:
    from engine.core.extract.router import _dropped_the_listing

    cls = classify(preclean(DIRECTORY, ExtractOptions()))
    assert _dropped_the_listing("nothing but prose about nothing", cls)

    # Built from the detected nodes, not hand-written: the repeating unit the
    # classifier finds is the inner description, so a guessed string tests the
    # assertion rather than the code.
    def _text(group: list) -> str:
        return " ".join((n.text(deep=True, strip=True) or "") for n in group)

    groups = sorted(cls.repeated_groups, key=len, reverse=True)
    cards, quotes = groups[0], groups[1]

    # Every qualifying group must be represented, not just the one you care
    # about: an output holding the cards but none of the quotes has still thrown
    # a block away.
    assert _dropped_the_listing(_text(cards), cls)
    assert _dropped_the_listing(_text(quotes), cls)
    assert not _dropped_the_listing(_text(cards) + " " + _text(quotes), cls)


@pytest.mark.parametrize("markdown", ["", None])
def test_an_empty_extraction_counts_as_dropped(markdown: str | None) -> None:
    from engine.core.extract.router import _dropped_the_listing

    cls = classify(preclean(DIRECTORY, ExtractOptions()))
    assert _dropped_the_listing(markdown, cls)


def test_a_page_with_no_repeated_group_is_never_flagged() -> None:
    """An ordinary article must not be dragged through the rescue path."""
    from engine.core.extract.router import _dropped_the_listing

    article = (
        "<!doctype html><html><body><main><h1>On Scraping</h1>"
        + "".join(f"<p>{'A sentence about extraction. ' * 8}</p>" for _ in range(6))
        + "</main></body></html>"
    )
    cls = classify(preclean(article, ExtractOptions()))
    assert not _dropped_the_listing("anything at all", cls)
