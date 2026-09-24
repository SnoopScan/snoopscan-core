"""The extraction fixture suite, run as tests.

04-extraction.md section 9 calls this non-negotiable. The reason is specific:
extraction regressions are SILENT — the output still looks like text, so
nothing fails and nobody notices until a consumer downstream is confidently
wrong about a page it never really read.

Every fixture asserts three things, because F1 alone hides real failures:
a score against a reference extraction, the content that must survive, and
the boilerplate that must not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.core.extract.code_blocks import language_of, restore, source_code_blocks
from engine.core.extract.quality import count_code_blocks, count_tables, score, tokenize
from engine.core.extract.router import extract
from engine.core.extract.structured import humanise_schema_enum
from engine.tests.fixtures.corpus import Fixture, all_fixtures

BASELINE = Path(__file__).resolve().parent / "baseline.json"

# A fixture may move a little as extraction changes; a real regression is
# larger than this.
TOLERANCE = 0.02


def run(fixture: Fixture) -> tuple[str, float]:
    result = extract(fixture.html, f"https://example.com/{fixture.name}")
    return result.markdown, score(result.markdown, fixture.reference).f1


@pytest.mark.parametrize("fixture", all_fixtures(), ids=lambda f: f.name)
def test_required_content_survives(fixture: Fixture) -> None:
    """Recall, checked directly. A high F1 can still hide a lost heading."""
    markdown, _ = run(fixture)
    for needle in fixture.must_contain:
        assert needle.lower() in markdown.lower(), (
            f"{fixture.name} lost required content {needle!r}. {fixture.note}"
        )


@pytest.mark.parametrize("fixture", all_fixtures(), ids=lambda f: f.name)
def test_boilerplate_is_removed(fixture: Fixture) -> None:
    """Precision, checked directly. Keeping nav and cookie notices is the
    complaint most often made about scraping output."""
    markdown, _ = run(fixture)
    for needle in fixture.must_not_contain:
        assert needle.lower() not in markdown.lower(), f"{fixture.name} kept boilerplate {needle!r}"


@pytest.mark.parametrize("fixture", all_fixtures(), ids=lambda f: f.name)
def test_structure_is_preserved(fixture: Fixture) -> None:
    """Tables and code are where structure-destroying extraction does the most
    damage, because the loss is invisible in plain text."""
    markdown, _ = run(fixture)
    assert count_tables(markdown) >= fixture.min_tables, f"{fixture.name} lost a table"
    assert count_code_blocks(markdown) >= fixture.min_code_blocks, (
        f"{fixture.name} lost a code block"
    )


@pytest.mark.parametrize("fixture", all_fixtures(), ids=lambda f: f.name)
def test_no_fixture_regressed_against_the_baseline(fixture: Fixture) -> None:
    """The recorded baseline is the contract. It should only ever go up."""
    stored = json.loads(BASELINE.read_text())
    previous = stored["fixtures"].get(fixture.name)
    if previous is None:
        pytest.skip(f"{fixture.name} is new; run --baseline to record it")
    _, current = run(fixture)
    assert current >= previous - TOLERANCE, (
        f"{fixture.name} regressed: {previous:.3f} -> {current:.3f}. {fixture.note}"
    )


def test_the_headline_number_holds() -> None:
    scores = [run(fixture)[1] for fixture in all_fixtures()]
    headline = sum(scores) / len(scores)
    previous = json.loads(BASELINE.read_text())["headline"]
    assert headline >= previous - TOLERANCE, (
        f"headline extraction F1 fell: {previous:.4f} -> {headline:.4f}"
    )


def test_the_structured_path_beats_the_heuristic_where_it_must() -> None:
    """The Phase 5 acceptance criterion, verbatim: forum and product must
    measurably beat the heuristic baseline."""
    from engine.core.extract.classify import classify
    from engine.core.extract.heuristic import HeuristicExtractor
    from engine.core.extract.router import ExtractOptions, preclean
    from engine.core.models import PageType

    for fixture in all_fixtures():
        if fixture.page_type not in (PageType.FORUM, PageType.PRODUCT):
            continue
        _, routed = run(fixture)

        options = ExtractOptions()
        cleaned = preclean(fixture.html, options)
        baseline = HeuristicExtractor().extract(
            cleaned, f"https://example.com/{fixture.name}", classify(cleaned), options
        )
        heuristic = score(baseline.markdown, fixture.reference).f1

        assert routed > heuristic, (
            f"{fixture.name}: structured {routed:.3f} does not beat heuristic "
            f"{heuristic:.3f} — the structured path is not earning its place"
        )


# --------------------------------------------------------------------------
# Scoring behaves
# --------------------------------------------------------------------------


def test_identical_text_scores_one() -> None:
    assert score("the cat sat", "the cat sat").f1 == 1.0


def test_disjoint_text_scores_zero() -> None:
    assert score("alpha beta", "gamma delta").f1 == 0.0


def test_markdown_syntax_is_not_scored_as_content() -> None:
    """Otherwise emitting more syntax would look like better extraction."""
    assert tokenize("## **Bold** heading") == tokenize("Bold heading")


def test_repetition_is_penalised() -> None:
    """An extractor that repeats one paragraph should not score as well as one
    that returns the page."""
    repeated = score("cat cat cat cat", "cat dog bird fish")
    assert repeated.f1 < 0.5


# --------------------------------------------------------------------------
# Code fidelity — regressions found on the docs fixture
# --------------------------------------------------------------------------


def test_a_single_line_code_block_stays_a_block() -> None:
    """It came back as INLINE code, so a consumer parsing fenced blocks would
    not have seen it at all."""
    html = "<pre><code class='language-bash'>pip install thing</code></pre>"
    repaired = restore("`pip install thing`", html)
    assert "```bash" in repaired
    assert "`pip install thing`" not in repaired.replace("```bash", "")


def test_a_dropped_language_hint_is_restored() -> None:
    html = "<pre><code class='language-python'>x = 1</code></pre>"
    assert "```python" in restore("```\nx = 1\n```", html)


def test_language_is_read_from_several_class_conventions() -> None:
    from selectolax.parser import HTMLParser

    for markup, expected in (
        ("<code class='language-python'>x</code>", "python"),
        ("<code class='lang-go'>x</code>", "go"),
        ("<code class='highlight-source-rust'>x</code>", "rust"),
        ("<code class='sql'>x</code>", "sql"),
    ):
        node = HTMLParser(markup).css_first("code")
        assert language_of(node) == expected


def test_a_highlighter_name_is_not_treated_as_a_language() -> None:
    from selectolax.parser import HTMLParser

    node = HTMLParser("<code class='highlight-js'>x</code>").css_first("code")
    assert language_of(node) == ""


def test_restore_invents_nothing() -> None:
    """Only content present in the source may be added back."""
    assert "invented" not in restore("some prose", "<p>no code here</p>")


def test_source_blocks_are_found_with_and_without_a_code_element() -> None:
    assert len(source_code_blocks("<pre><code>a</code></pre><pre>b</pre>")) == 2


# --------------------------------------------------------------------------
# Product markup normalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://schema.org/InStock", "In stock"),
        ("https://schema.org/OutOfStock", "Out of stock"),
        ("https://schema.org/PreOrder", "Pre-order"),
        ("http://schema.org/Discontinued", "Discontinued"),
    ],
)
def test_schema_enums_become_words(raw: str, expected: str) -> None:
    """A raw schema.org URL is not output a human or a model wants, and it
    pollutes every downstream consumer."""
    assert humanise_schema_enum(raw) == expected


def test_an_unknown_schema_enum_is_split_rather_than_dropped() -> None:
    assert humanise_schema_enum("https://schema.org/SomeNewState") == "Some New State"


def test_a_plain_value_passes_through_untouched() -> None:
    assert humanise_schema_enum("129.99") == "129.99"
