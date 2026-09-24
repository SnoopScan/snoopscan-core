"""Pre-clean must stay bounded on a large page.

The first version of `separate_adjacent_elements` walked the DOM and called
`insert_before(" ")`. It passed every test in the suite and was correct on
every page anyone tried by hand. On a 928 KB Etsy category page it left the
tree in a state whose SERIALISATION consumed unbounded memory: the process was
SIGKILLed (exit 137) and took the API down with it, twice.

Nothing in the suite exercised a page big enough to show it, so the tests here
are about SIZE and TIME rather than correctness — the two properties that were
silently violated. The implementation is now a string transform, which cannot
corrupt a tree at all, but the guard belongs to the behaviour rather than the
implementation: whatever pre-clean does later must still hold.
"""

from __future__ import annotations

import time

import pytest

from engine.core.extract.router import ExtractOptions, preclean, separate_adjacent_elements


def _big_page(cards: int = 4_000) -> str:
    """Minified markup with thousands of flush-adjacent elements — the shape
    that triggered it. Real listing pages look exactly like this."""
    body = "".join(
        f'<div class="card"><a href="/i/{i}">Item {i}</a><span>Shop {i}</span>'
        f"<p>A description of item {i}.</p></div>"
        for i in range(cards)
    )
    return f"<!doctype html><html><body><div class='grid'>{body}</div></body></html>"


def test_preclean_does_not_inflate_a_large_page() -> None:
    """The failure mode was unbounded growth, so assert on the growth."""
    html = _big_page()
    out = preclean(html, ExtractOptions())
    assert len(out) < len(html) * 1.5, (
        f"pre-clean grew the page from {len(html)} to {len(out)} bytes; "
        "the DOM-mutating version grew it without bound"
    )


def test_preclean_finishes_promptly_on_a_large_page() -> None:
    html = _big_page()
    started = time.monotonic()
    preclean(html, ExtractOptions())
    elapsed = time.monotonic() - started
    # Catching a quadratic blow-up, which costs minutes, not milliseconds —
    # so the bound is loose enough to survive a loaded box.
    assert elapsed < 15.0, f"pre-clean took {elapsed:.1f}s on a {len(html) // 1024} KB page"


@pytest.mark.parametrize("cards", [100, 1_000, 8_000])
def test_growth_is_linear_in_the_number_of_elements(cards: int) -> None:
    """A superlinear implementation is the one that killed the process."""
    html = _big_page(cards)
    out = separate_adjacent_elements(html)
    # One space per flush closing/opening pair, and no more.
    added = len(out) - len(html)
    assert 0 <= added <= cards * 4, f"{added} bytes added for {cards} cards"


def test_it_still_separates_the_pair_it_exists_for() -> None:
    out = separate_adjacent_elements("<a>English nouns</a><a>FictIf characters</a>")
    assert out == "<a>English nouns</a> <a>FictIf characters</a>"


def test_it_leaves_text_beside_markup_alone() -> None:
    """`<span>Name</span>berry` must stay one word."""
    assert separate_adjacent_elements("<span>Name</span>berry") == "<span>Name</span>berry"


def test_it_does_not_separate_an_opening_pair() -> None:
    """`<div><p>` needs no help — a browser boxes them separately anyway."""
    assert separate_adjacent_elements("<div><p>x</p></div>") == "<div><p>x</p></div>"
