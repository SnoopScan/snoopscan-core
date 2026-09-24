"""Templates: a named schema, filled from the page's own markup, free.

A caller who wants a price should not write a JSON schema, and should not pay
a model to read a number the page already publishes in JSON-LD. These pin the
part that makes that true: the fields come from the markup and the model is
never called.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from engine.core.extract import templates
from engine.core.extract.structured_json import extract_against_schema
from engine.core.models import ScrapeOptions

SHOP_PAGE_LD = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Tree Dasher 2",
    "brand": {"@type": "Brand", "name": "Allbirds"},
    "sku": "AB-TD2-41",
    "offers": {
        "@type": "Offer",
        "price": "125.00",
        "priceCurrency": "GBP",
        "availability": "https://schema.org/InStock",
    },
    "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.4", "reviewCount": "812"},
}

NEWS_PAGE_LD = {
    "@context": "https://schema.org",
    "@type": "NewsArticle",
    "headline": "Baby name apps are having a moment",
    "author": {"@type": "Person", "name": "Dana Reed"},
    "datePublished": "2026-09-01T09:00:00Z",
    "publisher": {"@type": "Organization", "name": "The Example Post"},
}


def _model_that_must_not_run(*args: Any, **kwargs: Any) -> str:
    raise AssertionError("a template must be answered by the markup, not by a model")


def test_every_template_is_listed_with_its_fields() -> None:
    names = {t["name"] for t in templates.catalogue()}
    assert names == set(templates.names())
    for entry in templates.catalogue():
        assert entry["description"] and entry["fields"], entry["name"]


def test_a_product_template_is_filled_from_the_shops_own_markup() -> None:
    outcome = extract_against_schema(
        markdown="Tree Dasher 2 ...",
        structured_hints=SHOP_PAGE_LD,
        schema=templates.schema_for("product") or {},
        model=_model_that_must_not_run,
    )
    assert outcome.error is None, outcome.error
    assert outcome.source == "markup", "the page answered it; no model should have run"
    assert outcome.data is not None
    assert outcome.data["name"] == "Tree Dasher 2"
    assert outcome.data["brand"] == "Allbirds"
    assert outcome.data["price"] == 125.0, "a price must be a number, not 'about £125'"
    assert outcome.data["currency"] == "GBP"
    assert outcome.data["ratingValue"] == 4.4


def test_an_article_template_is_filled_the_same_way() -> None:
    outcome = extract_against_schema(
        markdown="...",
        structured_hints=NEWS_PAGE_LD,
        schema=templates.schema_for("article") or {},
        model=_model_that_must_not_run,
    )
    assert outcome.source == "markup"
    assert outcome.data is not None
    assert outcome.data["headline"].startswith("Baby name apps")
    assert outcome.data["author"] == "Dana Reed"
    assert outcome.data["publisher"] == "The Example Post"


def test_extract_takes_a_template_the_same_way_the_json_format_does() -> None:
    """One rule, both endpoints. /extract is the schema-first endpoint, so a
    template failing there while working on scrape's json format is the
    surprise a caller hits first."""
    from engine.core.models import ExtractRequest

    req = ExtractRequest.model_validate({"urls": ["https://example.com/p"], "template": "product"})
    assert req.effective_schema == templates.schema_for("product")

    for bad in ({"template": "nope"}, {"template": "product", "schema": {"type": "object"}}, {}):
        with pytest.raises(ValidationError):
            ExtractRequest.model_validate({"urls": ["https://example.com/p"], **bad})


def test_asking_for_a_template_needs_no_schema() -> None:
    options = ScrapeOptions.model_validate({"formats": [{"type": "json", "template": "product"}]})
    spec = options.json_format
    assert spec is not None
    assert spec.effective_schema == templates.schema_for("product")


def test_a_hand_written_schema_still_works() -> None:
    mine = {"type": "object", "properties": {"colour": {"type": "string"}}}
    options = ScrapeOptions.model_validate({"formats": [{"type": "json", "schema": mine}]})
    assert options.json_format is not None
    assert options.json_format.effective_schema == mine


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "json"},
        {"type": "json", "template": "nope"},
        {"type": "json", "template": "product", "schema": {"type": "object"}},
    ],
)
def test_the_ambiguous_and_the_unknown_are_refused(spec: dict[str, Any]) -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic's own error type
        ScrapeOptions.model_validate({"formats": [spec]})


def test_every_template_names_schema_org_fields() -> None:
    """The reason they fill for free: sites publish these names already. A
    field invented here would need a model on every page."""
    shipped = templates.shipped()
    invented = {
        name: [f for f in entry["schema"]["properties"] if f != f.strip() or " " in f]
        for name, entry in shipped.items()
    }
    assert not any(invented.values()), invented
    assert json.dumps(shipped)  # serialisable, since it goes over the wire


# ------------------------------------------------------------------- drift
# A template is a promise about field names. It drifts two ways: the shipped
# file stops matching what sites publish, or a second copy of the list appears
# somewhere and the two disagree. Both are caught here rather than by a
# customer getting empty fields.


def test_the_shipped_file_is_the_only_copy_of_the_list() -> None:
    """No module may keep its own list of template names.

    A single name in another module is fine and common — `article` is also a
    page type in the classifier. Several of them together in one file is a
    second copy of the catalogue, which is what drifts.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    shipped = set(templates.shipped())
    offenders = []
    for path in list((root / "core").rglob("*.py")) + list((root / "api").rglob("*.py")):
        if path.name == "templates.py":
            continue
        text = path.read_text(encoding="utf-8")
        named = {n for n in shipped if f'"{n}"' in text or f"'{n}'" in text}
        if len(named) >= 3:
            offenders.append(f"{path.relative_to(root)}: {sorted(named)}")
    assert not offenders, f"a second copy of the template catalogue: {offenders}"


def test_a_desk_template_overrides_a_shipped_one_and_falls_back() -> None:
    """The update path: the desk can replace a shipped template by name, and
    removing theirs brings the shipped one back."""
    original = templates.schema_for("product")
    assert original is not None
    mine = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "colourway": {"type": "string"}},
    }
    templates._OVERRIDES["product"] = {
        "name": "product",
        "description": "ours",
        "schema": mine,
        "source": "desk",
    }
    try:
        assert templates.schema_for("product") == mine
        assert [t for t in templates.catalogue() if t["name"] == "product"][0]["source"] == "desk"
    finally:
        templates.invalidate()
    assert templates.schema_for("product") == original, (
        "removing the desk's brings the shipped back"
    )


@pytest.mark.parametrize("name", sorted(templates.shipped()))
def test_every_shipped_template_still_fills_from_schema_org_markup(name: str) -> None:
    """The drift check with teeth: build the markup a site would publish for
    this type, and assert the template reads it. If schema.org renames a field,
    or ours stops matching, this fails here instead of returning empty fields
    to a customer."""
    schema = templates.schema_for(name) or {}
    properties = schema.get("properties") or {}
    # A page publishing exactly these names, in the shapes sites really use.
    markup: dict[str, Any] = {"@context": "https://schema.org", "@type": name}
    for field, spec in properties.items():
        kind = spec.get("type")
        markup[field] = (
            "12.5"
            if kind == "number"
            else "12"
            if kind == "integer"
            else ["one", "two"]
            if kind == "array"
            else True
            if kind == "boolean"
            else f"{field} value"
        )
    outcome = extract_against_schema(
        markdown="", structured_hints=markup, schema=schema, model=_model_that_must_not_run
    )
    assert outcome.error is None, f"{name}: {outcome.error}"
    assert outcome.data is not None
    missing = [f for f in properties if f not in outcome.data]
    assert not missing, f"{name}: template fields no longer read from markup: {missing}"


def test_a_count_comes_back_as_a_count() -> None:
    """107 reviews, not 107.0 of them. Declared as `number`, every count in
    every answer carried a decimal point (measured live, 20 Sep 2026)."""
    outcome = extract_against_schema(
        markdown="",
        structured_hints={
            "@type": "Product",
            "name": "Tree Dasher 2",
            "aggregateRating": {"ratingValue": "4.5", "reviewCount": "107"},
        },
        schema=templates.schema_for("product") or {},
        model=_model_that_must_not_run,
    )
    assert outcome.data is not None
    assert outcome.data["reviewCount"] == 107
    assert isinstance(outcome.data["reviewCount"], int)
    assert outcome.data["ratingValue"] == 4.5, "a rating IS fractional and stays so"
