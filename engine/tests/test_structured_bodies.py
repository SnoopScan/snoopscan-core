"""A JSON or XML body is data, not a blocked page.

Found by using our own engine to research competitors on the keyless GitHub API
(6 Sep 2026): the fetch returned 200, the validator scored the response
SOFT_BLOCK on the extraction confidence floor because it had no prose and no
links, and that verdict went into the domain profile. Fourteen API URLs raised
the tier and opened the circuit on `api.github.com`, after which nothing on that
host could be fetched at all — including ordinary HTML pages.

That is the shape that matters: the cost is not one failed fetch, it is a whole
host written off. Sitemaps, RSS, `llms.txt` and every "ask the platform for its
data" endpoint are in the same class.
"""

from __future__ import annotations

import pytest

from engine.core.detect.validator import (
    ExtractionSummary,
    is_structured_data,
    validate,
)
from engine.core.fetch.base import FetchResult


def _result(body: bytes, content_type: str, status: int = 200) -> FetchResult:
    return FetchResult(
        url="https://api.example.com/v1/things",
        status_code=status,
        headers={"content-type": content_type},
        body=body,
        content_type=content_type,
        latency_ms=80,
        bytes_transferred=len(body),
        tier="http",
        error=None,
    )


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "application/json; charset=utf-8",
        "application/xml",
        "text/xml",
        "application/rss+xml",
        "application/atom+xml",
        "application/vnd.api+json",
    ],
)
def test_a_structured_body_is_never_a_block(content_type: str) -> None:
    """No prose, no links, no site furniture — correct for an API response."""
    body = b'[{"id":1,"name":"thing"}]'
    summary = ExtractionSummary(word_count=3, char_count=len(body), link_count=0)
    verdict = validate(_result(body, content_type), None, summary)
    assert verdict.ok, f"{content_type} judged {verdict.reason}/{verdict.signal}"


def test_the_same_body_as_html_is_still_judged() -> None:
    """The negative control: this must not become a blanket amnesty.

    An HTML page with the same emptiness is exactly what a challenge page looks
    like, and must still be caught.
    """
    body = b"<html><body></body></html>"
    summary = ExtractionSummary(word_count=3, char_count=len(body), link_count=0)
    verdict = validate(_result(body, "text/html"), None, summary)
    assert not verdict.ok, "an empty HTML page must still be judged"


def test_an_error_status_is_still_an_error_when_it_is_json() -> None:
    """APIs return JSON error bodies. A 403 is a 403 whatever it is dressed as —
    the status layer runs before the structured-body shortcut."""
    body = b'{"message":"Forbidden"}'
    summary = ExtractionSummary(word_count=2, char_count=len(body), link_count=0)
    verdict = validate(_result(body, "application/json", status=403), None, summary)
    assert not verdict.ok


def test_an_empty_structured_body_is_not_waved_through() -> None:
    """A zero-byte 200 is a transport problem whatever the header claims."""
    summary = ExtractionSummary(word_count=0, char_count=0, link_count=0)
    verdict = validate(_result(b"", "application/json"), None, summary)
    assert not verdict.ok


@pytest.mark.parametrize(
    "content_type,expected",
    [
        ("application/json", True),
        ("application/vnd.github+json", True),
        ("text/xml", True),
        ("text/html", False),
        ("text/plain", False),
        ("application/pdf", False),
        (None, False),
    ],
)
def test_which_content_types_count_as_structured(content_type: str | None, expected: bool) -> None:
    assert is_structured_data(content_type) is expected


def test_a_markup_list_stays_a_list_under_its_own_key() -> None:
    """Recipes publish their ingredients and method as lists. Both used to
    vanish, and a step's `text` surfaced as a top-level key where a schema
    could match it by accident (20 Sep 2026)."""
    from engine.core.extract.structured_json import _flatten

    flat = _flatten(
        {
            "@type": "Recipe",
            "name": "Onion soup",
            "recipeIngredient": ["2 onions", "1 stock cube"],
            "recipeInstructions": [
                {"@type": "HowToStep", "text": "Chop the onions."},
                {"@type": "HowToStep", "text": "Simmer for an hour."},
            ],
            "author": {"@type": "Person", "name": "Nigel"},
            "offers": [{"@type": "Offer", "price": "9.99", "priceCurrency": "GBP"}],
        }
    )
    assert flat["recipeIngredient"] == ["2 onions", "1 stock cube"]
    assert flat["recipeInstructions"] == ["Chop the onions.", "Simmer for an hour."]
    assert flat["name"] == "Onion soup", "the recipe's name, not an ingredient's"
    assert flat["author"] == "Nigel", "an entity still stands for its name"
    assert flat["price"] == "9.99", "a list is still descended into for its keys"


def test_a_page_with_several_json_ld_blocks_still_reads_every_field() -> None:
    """Three blocks arrive as {"@graph": [...]}, and each block has a `name`.
    Reading that list as a list of names swallowed the whole graph and every
    field went missing — a recipe page returned {} live (20 Sep 2026)."""
    from engine.core.extract.structured_json import _flatten

    flat = _flatten(
        {
            "@graph": [
                {
                    "@type": "Recipe",
                    "name": "Classic lasagne",
                    "recipeIngredient": ["500g beef mince", "1 onion"],
                    "author": {"@type": "Person", "name": "Angela"},
                },
                {"@type": "BreadcrumbList", "itemListElement": []},
                {"@type": "WebPage", "aggregateRating": {"ratingValue": "4.6"}},
            ]
        }
    )
    assert flat["name"] == "Classic lasagne"
    assert flat["recipeIngredient"] == ["500g beef mince", "1 onion"]
    assert flat["author"] == "Angela"
    assert flat["ratingValue"] == "4.6", "a field from a later block still lifts"
