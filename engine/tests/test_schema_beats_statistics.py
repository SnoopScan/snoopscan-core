"""A page that SAYS what it is outranks a guess about what it looks like.

Allbirds (Shopify), 6 Sep 2026: a product page declaring JSON-LD
`@type: ProductGroup` and `og:type=product`, and a collection page declaring
`CollectionPage`, were both classified FORUM — the repeated-block heuristic
scored 4.0, and neither declaration scored anything, because the classifier
only knew `Product`. The forum path then stamped `## Post 1`, plausibility
fired on the repetition, and a real store page was reported BLOCKED at every
tier after paying for a browser render.
"""

from __future__ import annotations

from engine.core.extract.classify import PageType, classify
from engine.tests.fixtures.platforms import load

# Enough near-identical blocks to make the forum heuristic fire, as the
# Allbirds pages do (size guides and policy drawers repeated per product).
# Author and time hints make the repeated unit read as posts, which is what the
# forum heuristic keys on and what Allbirds' policy drawers happened to contain.
_REPEATS = "".join(
    f"<section><h2>Refund policy</h2><p>by Support Team · 2 hours ago</p>"
    f"<p>Our standard return policy is 30 days. Item {i} may be returned unworn with "
    f"tags attached within 30 days of delivery.</p></section>"
    for i in range(12)
)


def _page(ld_type: str, og: str | None) -> str:
    og_tag = f'<meta property="og:type" content="{og}">' if og else ""
    return (
        "<html><head>"
        + og_tag
        + '<script type="application/ld+json">'
        + '{"@context":"https://schema.org/","@type":"'
        + ld_type
        + '","name":"Tree Runner",'
        + '"brand":{"@type":"Brand","name":"Allbirds"},"offers":{"@type":"Offer","price":"98.00",'
        + '"priceCurrency":"USD","availability":"https://schema.org/InStock"}}'
        + "</script></head><body><h1>Tree Runner</h1>"
        + _REPEATS
        + "</body></html>"
    )


def test_a_product_group_declaration_makes_a_product_page() -> None:
    cls = classify(_page("ProductGroup", "product"))
    assert cls.page_type == PageType.PRODUCT, cls.scores


def test_og_type_product_alone_beats_the_forum_guess() -> None:
    cls = classify(_page("Thing", "product"))
    assert cls.page_type == PageType.PRODUCT, cls.scores


def test_a_collection_page_declaration_makes_a_listing() -> None:
    cls = classify(_page("CollectionPage", "website"))
    assert cls.page_type == PageType.LISTING, cls.scores


def test_an_item_list_declaration_makes_a_listing() -> None:
    cls = classify(_page("ItemList", None))
    assert cls.page_type == PageType.LISTING, cls.scores


def test_a_page_with_no_declaration_still_gets_the_statistical_answer() -> None:
    """The heuristic is not switched off — only outranked when the page speaks."""
    cls = classify(_page("Thing", None))
    assert cls.page_type != PageType.PRODUCT, cls.scores
    assert cls.scores.get(PageType.PRODUCT, 0.0) < 3.0, "nothing declared, nothing awarded"


# --------------------------------------------------------------------------
# The real pages, as captured
# --------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

_FIX = Path(__file__).parent / "fixtures" / "platforms"


def test_the_real_allbirds_product_page_is_a_product() -> None:
    html = load("shopify_product_page.html")
    cls = classify(html)
    assert cls.page_type == PageType.PRODUCT, cls.scores


def test_the_real_allbirds_collection_page_is_a_listing() -> None:
    html = load("shopify_collection_page.html")
    cls = classify(html)
    assert cls.page_type == PageType.LISTING, cls.scores


def test_the_real_product_page_validates_as_content() -> None:
    """The whole chain: it was BLOCKED at every tier after a browser render."""
    from engine.core.detect.validator import DomainStats, ExtractionSummary, extract_title, validate
    from engine.core.extract.router import ExtractOptions, extract
    from engine.core.fetch.base import FetchResult

    html = load("shopify_product_page.html")
    url = "https://www.allbirds.com/products/free-returns-coverage"
    ext = extract(html, url, ExtractOptions())
    assert "## Post " not in ext.markdown, "a product page is not a forum"
    result = FetchResult(
        url=url,
        status_code=200,
        headers={"content-type": "text/html"},
        body=html.encode(),
        content_type="text/html",
        tier="http",
        latency_ms=1,
        bytes_transferred=len(html),
    )
    summary = ExtractionSummary(
        markdown=ext.markdown,
        page_type=str(ext.page_type),
        word_count=ext.word_count,
        char_count=ext.char_count,
        confidence=ext.confidence,
        link_count=len(ext.links),
        title=ext.title or extract_title(html),
    )
    verdict = validate(result, DomainStats(domain="allbirds.com"), summary)
    assert verdict.ok, f"{verdict.signal}: {dict(verdict.details)}"
