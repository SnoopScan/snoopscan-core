"""A confidence number has to be about the answer, not about the code path.

Measured against quotes.toscrape.com through `/v1/extract`, 9 Sep 2026:

    {"url": "...", "data": {}, "confidence": 0.95, "source": "markup"}

Nothing was found. The page has no structured markup and the schema asked for
a title. The 0.95 was a constant attached to the markup BRANCH, so it said
"near certain" about an empty object, and a caller reading it cannot tell
"we are sure the page says this" from "we are sure of nothing".

Confidence is now the source's own trust multiplied by how much of the schema
came back, and an extraction that found nothing is an error rather than an
empty object wearing a number.
"""

from __future__ import annotations

from engine.core.extract.structured_json import (
    NOTHING_FOUND,
    coverage,
    extract_against_schema,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "price": {"type": "number"},
        "sku": {"type": "string"},
    },
}

MARKUP = {"@type": "Product", "name": "Pilot ladder", "price": 129.0, "sku": "PL-9"}


def test_nothing_found_is_not_a_confident_empty_object() -> None:
    out = extract_against_schema(markdown="", structured_hints=None, schema=SCHEMA)

    assert out.data is None, "an empty object is not an extraction"
    assert out.confidence == 0.0
    assert out.error == NOTHING_FOUND


def test_everything_found_keeps_the_source_confidence() -> None:
    out = extract_against_schema(markdown="", structured_hints=MARKUP, schema=SCHEMA)

    assert out.data == {"name": "Pilot ladder", "price": 129.0, "sku": "PL-9"}
    assert out.confidence == 0.95
    assert out.source == "markup"


def test_a_partial_answer_reports_a_partial_confidence() -> None:
    """One field of three. Same source, same code path, a different answer —
    and it is the ANSWER the number is about."""
    partial = {"@type": "Product", "name": "Pilot ladder"}

    out = extract_against_schema(markdown="", structured_hints=partial, schema=SCHEMA)

    assert out.data == {"name": "Pilot ladder"}
    assert 0.3 < out.confidence < 0.35, out.confidence
    assert out.confidence < 0.95, "a third of the schema must not read as certainty"


def test_a_schema_with_no_properties_asks_for_everything() -> None:
    """Nothing to count, so the source's own confidence stands unchanged."""
    assert coverage({}, {"type": "object"}) == 1.0
    assert coverage({"anything": 1}, {"type": "object"}) == 1.0


def test_coverage_counts_nulls_as_absent() -> None:
    """A key present with a null value has not been answered."""
    assert coverage({"name": None, "price": 1, "sku": None}, SCHEMA) == 1 / 3
