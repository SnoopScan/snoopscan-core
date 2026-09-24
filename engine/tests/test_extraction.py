"""Extraction and classification.

Extraction regressions are silent — the output still looks like text — so
every assertion here checks something specific: word count in range, required
content present, forbidden boilerplate absent, structure preserved.
"""

from __future__ import annotations

import pytest

from engine.core.extract.boilerplate import sweep
from engine.core.extract.classify import classify, find_repeated_blocks
from engine.core.extract.confidence import ConfidenceInputs, score
from engine.core.extract.router import ExtractOptions, extract
from engine.core.models import PageType
from engine.tests.fixtures.builders import (
    article_html,
    docs_html,
    forum_html,
    listing_html,
    product_html,
    table_html,
)

# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (article_html(), PageType.ARTICLE),
        (forum_html(), PageType.FORUM),
        (listing_html(), PageType.LISTING),
        (product_html(), PageType.PRODUCT),
        (table_html(), PageType.TABLE),
        (docs_html(), PageType.DOCS),
    ],
)
def test_classifier_routes_correctly(html: str, expected: PageType) -> None:
    """Misclassification routes to the wrong extractor and is invisible unless
    tested directly."""
    assert classify(html).page_type == expected


def test_json_ld_product_is_the_strongest_signal() -> None:
    result = classify(product_html())
    assert result.page_type == PageType.PRODUCT
    assert result.structured_data, "JSON-LD should be parsed, not ignored"


def test_repeated_block_detection_finds_the_unit() -> None:
    from selectolax.parser import HTMLParser

    signature, count, nodes = find_repeated_blocks(HTMLParser(forum_html()))
    assert signature is not None
    assert count >= 5
    assert len(nodes) == count


# --------------------------------------------------------------------------
# Heuristic path
# --------------------------------------------------------------------------


def test_article_extraction() -> None:
    result = extract(article_html(), "https://example.com/post")
    assert result.page_type == PageType.ARTICLE
    assert result.title == "How Extraction Actually Works"
    assert result.word_count > 80
    assert "genuine body paragraph" in result.markdown
    assert result.confidence > 0.5


def test_article_strips_navigation_and_cookie_banner() -> None:
    result = extract(article_html(), "https://example.com/post")
    assert "we use cookies" not in result.markdown.lower()
    assert "Subscribe to our newsletter" not in result.markdown


def test_docs_preserves_code_blocks() -> None:
    result = extract(docs_html(), "https://example.com/docs/start")
    assert "```" in result.markdown, "fenced code must survive extraction"
    assert "pip install" in result.markdown


# --------------------------------------------------------------------------
# Structured path — the differentiator
# --------------------------------------------------------------------------


def test_forum_becomes_one_section_per_post() -> None:
    result = extract(forum_html(), "https://forum.example.com/t/1")
    assert result.page_type == PageType.FORUM
    assert str(result.extraction_path) == "structured"
    assert result.structured is not None
    assert result.structured["count"] == 5

    # Every author appears as a heading, and no post is lost.
    for i in range(1, 6):
        assert f"author{i}" in result.markdown
        assert f"body of post {i}" in result.markdown
    assert result.markdown.count("## ") == 5


def test_forum_keeps_quote_nesting() -> None:
    result = extract(forum_html(), "https://forum.example.com/t/1")
    assert ">" in result.markdown, "quoted text should survive as a blockquote"


def test_listing_produces_titled_linked_entries() -> None:
    result = extract(listing_html(), "https://shop.example.com/all")
    assert result.page_type == PageType.LISTING
    assert result.structured is not None
    assert result.structured["count"] == 8
    items = result.structured["items"]
    assert all(item["title"] for item in items)
    assert all(item["url"] for item in items)


def test_product_pulls_fields_from_structured_markup() -> None:
    """Structured markup is free and often answers directly — parse it before
    reaching for anything expensive."""
    result = extract(product_html(), "https://shop.example.com/p/1")
    assert result.structured is not None
    fields = result.structured["fields"]
    assert fields["name"] == "Mechanical Keyboard"
    assert str(fields["price"]) == "129.99"
    assert fields["currency"] == "GBP"


def test_table_becomes_a_markdown_table() -> None:
    result = extract(table_html(), "https://example.com/data")
    assert "| --- |" in result.markdown
    assert "| Widget A |" in result.markdown
    # Header row preserved.
    assert result.markdown.strip().startswith("| Product")


def test_library_converter_is_actually_used_not_silently_falling_back() -> None:
    """The structure-preserving converter must really be html-to-markdown.

    This exists because the integration once broke against a library API
    change and every other test still passed: the crude fallback converter
    quietly took over and produced output that still looked like markdown.
    A silent downgrade in extraction quality is the hardest kind to notice.
    """
    from engine.core.extract.structured import _library_convert

    result = _library_convert(
        "<div><h2>Title</h2><p>Some <b>bold</b> text</p>"
        "<ul><li>one</li><li><em>two</em></li></ul>"
        '<pre><code class="language-python">x = 1</code></pre></div>'
    )
    assert result is not None, "html-to-markdown is not being used at all"
    # Inline emphasis and fence languages are exactly what the fallback loses.
    assert "**bold**" in result
    assert "*two*" in result or "_two_" in result
    assert "```python" in result


def test_complex_table_renders_as_markdown_keeping_its_content() -> None:
    """DECISION REVERSED 7 Sep 2026 — this test asserted the opposite.

    A merged-cell table used to be emitted as raw HTML, on the reasoning that
    destroying the structure is worse than embedding it. Measured against two
    real pages, that trade is the wrong way round: hetzner.com's product matrix
    arrived as 71 KB of `<table>` in the `markdown` field, and because tags
    count as words it beat the readable extraction and went to the caller.
    The colspan is now expanded across the columns it covers.
    """
    html = (
        "<html><head><title>Complex</title></head><body><table>"
        "<tr><th>A</th><th colspan='2'>Merged</th></tr>"
        "<tr><td>1</td><td>2</td><td>3</td></tr>"
        "<tr><td>4</td><td>5</td><td>6</td></tr>"
        "<tr><td>7</td><td>8</td><td>9</td></tr>"
        "</table></body></html>"
    )
    result = extract(html, "https://example.com/complex")
    assert "colspan" not in result.markdown, "no raw HTML in a markdown field"
    assert "Merged" in result.markdown and "9" in result.markdown


# --------------------------------------------------------------------------
# Selector filtering
# --------------------------------------------------------------------------


def test_exclude_tags_applied_to_source_dom() -> None:
    result = extract(
        article_html(),
        "https://example.com/post",
        ExtractOptions(exclude_tags=["article"]),
    )
    assert "genuine body paragraph" not in result.markdown


def test_include_tags_narrows_to_selection() -> None:
    result = extract(
        article_html(),
        "https://example.com/post",
        ExtractOptions(include_tags=["article"]),
    )
    assert "genuine body paragraph" in result.markdown
    assert "Subscribe to our newsletter" not in result.markdown


# --------------------------------------------------------------------------
# Boilerplate sweep — must not eat real text
# --------------------------------------------------------------------------


def test_sweep_removes_cookie_line() -> None:
    assert "we use cookies" not in sweep("# Title\n\nWe use cookies to improve.\n\nReal text.")


def test_sweep_leaves_prose_that_mentions_cookies() -> None:
    """Removing genuine content is worse than leaving a stray cookie line."""
    prose = (
        "The regulation requires that we use cookies only with consent, which "
        "changed how publishers across the industry approached their consent "
        "flows during the following eighteen months of enforcement activity."
    )
    assert "regulation requires" in sweep(f"# Title\n\n{prose}\n")


def test_sweep_drops_navigation_runs() -> None:
    markdown = "# Title\n\n[Home](/)\n[About](/a)\n[Contact](/c)\n\nReal content here.\n"
    result = sweep(markdown)
    assert "[Home](/)" not in result
    assert "Real content here." in result


def test_sweep_keeps_a_short_link_pair() -> None:
    """Two links are a reference, not a nav bar."""
    markdown = "# Title\n\n[Source](/s)\n[Docs](/d)\n\nBody text.\n"
    result = sweep(markdown)
    assert "[Source](/s)" in result


def test_sweep_trims_trailing_related_section_only_at_the_tail() -> None:
    body = "\n\n".join(f"Paragraph {i} of the real article body." for i in range(12))
    markdown = f"# Title\n\n{body}\n\n## Related articles\n\n- [Other](/o)\n"
    result = sweep(markdown)
    assert "Related articles" not in result
    assert "Paragraph 11" in result


def test_sweep_keeps_an_early_related_heading() -> None:
    """The same heading a third of the way down is page structure, not
    recirculation."""
    tail = "\n\n".join(f"Paragraph {i} continues the article." for i in range(20))
    markdown = f"# Title\n\nIntro.\n\n## Related articles\n\n{tail}\n"
    assert "Related articles" in sweep(markdown)


# --------------------------------------------------------------------------
# Confidence scoring
# --------------------------------------------------------------------------


def test_confidence_penalises_extracting_everything() -> None:
    """A ratio near 1.0 means we kept the nav too — a precision failure."""
    high_ratio = score(
        ConfidenceInputs(extracted_chars=9990, raw_text_chars=10_000, has_title=True)
    )
    healthy = score(ConfidenceInputs(extracted_chars=5000, raw_text_chars=10_000, has_title=True))
    assert high_ratio < healthy


def test_confidence_penalises_extracting_almost_nothing() -> None:
    low = score(ConfidenceInputs(extracted_chars=100, raw_text_chars=10_000, has_title=True))
    healthy = score(ConfidenceInputs(extracted_chars=5000, raw_text_chars=10_000, has_title=True))
    assert low < healthy


def test_confidence_penalises_lost_structure() -> None:
    lost = score(
        ConfidenceInputs(
            extracted_chars=5000,
            raw_text_chars=10_000,
            source_tables=4,
            output_tables=0,
            has_title=True,
        )
    )
    kept = score(
        ConfidenceInputs(
            extracted_chars=5000,
            raw_text_chars=10_000,
            source_tables=4,
            output_tables=4,
            has_title=True,
        )
    )
    assert lost < kept


def test_confidence_penalises_surviving_boilerplate() -> None:
    dirty = score(ConfidenceInputs(extracted_chars=5000, raw_text_chars=10_000, boilerplate_hits=5))
    clean = score(ConfidenceInputs(extracted_chars=5000, raw_text_chars=10_000))
    assert dirty < clean


def test_confidence_is_bounded() -> None:
    perfect = score(
        ConfidenceInputs(
            extracted_chars=5000,
            raw_text_chars=10_000,
            has_title=True,
            has_language=True,
            baseline_mean=5000,
            baseline_stdev=200,
        )
    )
    assert 0.0 <= perfect <= 1.0
    assert perfect > 0.9
