"""What a browser does not show, the extractor does not extract (6 Sep 2026).

Allbirds' product page carried 13,593 words, of which the product was a few
dozen: every policy lives in a <div role="dialog"> modal on every page. Framer
renders each section once per breakpoint, aria-hidden on all but one. Both are
the same fault — markup that is present but not shown — and both are fixed at
pre-clean rather than deduplicated afterwards.
"""

from __future__ import annotations

from pathlib import Path

from engine.core.extract.router import ExtractOptions, extract, preclean
from engine.tests.fixtures.platforms import load

FIX = Path(__file__).parent / "fixtures"


def test_the_real_allbirds_product_page_is_the_product_not_its_policies() -> None:
    html = load("shopify_product_page.html")
    ext = extract(html, "https://www.allbirds.com/products/free-returns-coverage", ExtractOptions())
    assert ext.word_count < 2_500, f"{ext.word_count} words: the modals are still in"
    assert "Change in Ownership and Operation" not in ext.markdown, "that is the privacy policy"


def test_role_dialog_and_hidden_are_dropped_but_open_dialogs_stay() -> None:
    html = (
        "<html><body><main><p>Visible copy about the product.</p></main>"
        '<div role="dialog"><p>MODAL-TEXT</p></div>'
        "<div hidden><p>HIDDEN-TEXT</p></div>"
        '<section aria-hidden="true"><p>ARIA-TEXT</p></section>'
        "<dialog><p>CLOSED-DIALOG</p></dialog>"
        "<dialog open><p>OPEN-DIALOG</p></dialog>"
        "</body></html>"
    )
    cleaned = preclean(html, ExtractOptions())
    for gone in ("MODAL-TEXT", "HIDDEN-TEXT", "ARIA-TEXT", "CLOSED-DIALOG"):
        assert gone not in cleaned, gone
    assert "OPEN-DIALOG" in cleaned and "Visible copy" in cleaned


def test_json_ld_inside_a_stripped_region_would_be_lost_so_it_is_read_first() -> None:
    """Classification reads JSON-LD from the cleaned tree; a <script> is never
    inside a dialog in practice, but the product schema must survive pre-clean."""
    html = (
        '<html><head><script type="application/ld+json">{"@type":"Product","name":"X"}</script>'
        '</head><body><div role="dialog">noise</div><h1>X</h1></body></html>'
    )
    assert '"@type":"Product"' in preclean(html, ExtractOptions())


# ------------------------------------------------------------------------
# aria-hidden on the page ITSELF means a modal is open, not "not content".
#
# When a site opens a pop-up — a cart drawer, a region picker, a newsletter
# offer — its script marks the rest of the page aria-hidden="true" so a screen
# reader stays in the pop-up. A request that waits for window load (the
# `network` format does) sees the page in exactly that state: a retail home
# page came back with <main aria-hidden="true">, pre-clean stripped it, and a
# 97,000-character page returned 0 words as a success (Sep 2026).


def _behind_a_modal(main_attrs: str = ' aria-hidden="true"') -> str:
    return (
        "<html><body>"
        '<header aria-hidden="true"><nav>Shop Men Women</nav></header>'
        f"<main{main_attrs}><h1>Spring collection</h1>"
        + "".join(
            f"<p>Real page copy, paragraph {i}, about item {i} in the collection "
            f"that a visitor came to read, in its own words.</p>"
            for i in range(20)
        )
        + "</main>"
        '<div role="dialog" aria-modal="true"><p>MODAL-OFFER Get 10% off</p></div>'
        "</body></html>"
    )


def test_main_marked_hidden_by_an_open_modal_is_still_the_page() -> None:
    cleaned = preclean(_behind_a_modal(), ExtractOptions())
    assert "Spring collection" in cleaned
    assert "Real page copy" in cleaned
    assert "MODAL-OFFER" not in cleaned, "the pop-up itself is still not content"


def test_a_wrapper_hidden_around_main_is_kept_too() -> None:
    html = (
        '<html><body><div id="app" aria-hidden="true"><main><h1>Title</h1>'
        + "<p>Body copy of the page, plenty of it to read here.</p>" * 20
        + "</main></div></body></html>"
    )
    cleaned = preclean(html, ExtractOptions())
    assert "Body copy of the page" in cleaned


def test_role_main_counts_as_main() -> None:
    html = (
        '<html><body><div role="main" aria-hidden="true"><h1>Docs</h1>'
        + "<p>Documentation text a reader came for.</p>" * 20
        + "</div></body></html>"
    )
    assert "Documentation text" in preclean(html, ExtractOptions())


def test_a_hidden_attribute_on_main_is_still_obeyed() -> None:
    """`hidden` is not a modal side effect: the browser genuinely does not
    render it. Only aria-hidden gets the benefit of the doubt."""
    html = "<html><body><main hidden><p>NOT-RENDERED</p></main><p>shown</p></body></html>"
    assert "NOT-RENDERED" not in preclean(html, ExtractOptions())


def test_the_whole_page_behind_a_modal_extracts_to_words() -> None:
    ext = extract(_behind_a_modal(), "https://shop.example.com/", ExtractOptions())
    assert ext.word_count > 100
