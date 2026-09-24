"""The pricing-research findings (measured, Sep 2026).

§10 said `onlyMainContent` guts JS-rendered pages. Reproducing it found
something else underneath: the shared cache was keyed on the URL, country and
mobile flag, and on NOTHING about how the page was extracted. One `pages` row
holds one extraction, so the first caller's shape was served to every caller
after them — measured on a page with no history, a request for the whole page
came back as somebody else's 6-character `includeTags: ["h1"]` result, marked
`cached: true`.
"""

from __future__ import annotations

from typing import Any

from engine.api.app import _problem
from engine.core.models import ScrapeOptions
from engine.core.scrape_service import ScrapeService

URL = "https://example.com/pricing"


def _key(**opts: Any) -> bytes:
    return ScrapeService._cache_variant(URL, ScrapeOptions(**opts))[0]


# --------------------------------------------------------------------------
# §10 (underneath): the cache must not serve a shape nobody asked for
# --------------------------------------------------------------------------


def test_extraction_options_change_the_cache_key() -> None:
    baseline = _key()
    assert _key(onlyMainContent=False) != baseline
    assert _key(includeTags=["h1"]) != baseline
    assert _key(excludeTags=["nav"]) != baseline
    assert _key(removeBase64Images=False) != baseline


def test_the_same_request_still_shares_one_row() -> None:
    """A cache that never hits is not a cache. Order must not matter either."""
    assert _key(includeTags=["h1", "h2"]) == _key(includeTags=["h2", "h1"])
    assert _key(onlyMainContent=True) == _key(onlyMainContent=True)


def test_formats_do_not_split_the_cache() -> None:
    """The row stores markdown, html, rawHtml and links together; choosing
    among them is not a different document, and splitting on it would multiply
    the cache for no correctness gain."""
    assert _key(formats=["markdown"]) == _key(formats=["markdown", "html", "links"])


def test_fetch_shaping_options_still_split_it() -> None:
    """The country/mobile behaviour this fix was modelled on must survive."""
    from engine.core.models import Location

    assert _key(mobile=True) != _key(mobile=False)
    assert _key(location=Location(country="GB")) != _key()


def test_a_personalised_fetch_is_never_shareable() -> None:
    _, shareable = ScrapeService._cache_variant(URL, ScrapeOptions(headers={"Cookie": "s=1"}))
    assert shareable is False
    assert ScrapeService._cache_variant(URL, ScrapeOptions())[1] is True


def test_the_extraction_variant_is_derived_from_the_options() -> None:
    """Spelled out so the failure mode is named: a new extraction option that
    is not in this string is a new way to serve the wrong document."""
    v = ScrapeService._extraction_variant(ScrapeOptions(includeTags=["main"]))
    assert "main=1" in v and "inc=main" in v and "exc=" in v and "b64=1" in v


# --------------------------------------------------------------------------
# §11 a misplaced field is misplaced, not unknown
# --------------------------------------------------------------------------


def test_a_scrape_option_at_the_top_level_is_named_as_misplaced() -> None:
    for field in ("formats", "onlyMainContent", "includeTags", "timeout"):
        out = _problem(
            {
                "loc": ("body", field),
                "msg": "Extra inputs are not permitted",
                "type": "extra_forbidden",
            },
            # The endpoint the finding was about: batch takes these one level
            # down. The hint is now given only where scrapeOptions exists.
            "/v1/batch/scrape",
        )
        assert "scrapeOptions" in out["message"], field
        assert field in out["message"]


def test_a_genuinely_unknown_field_keeps_the_plain_message() -> None:
    out = _problem(
        {
            "loc": ("body", "wibble"),
            "msg": "Extra inputs are not permitted",
            "type": "extra_forbidden",
        }
    )
    assert out["message"] == "Extra inputs are not permitted"


def test_other_validation_errors_are_untouched() -> None:
    out = _problem(
        {"loc": ("body", "limit"), "msg": "Input should be a valid integer", "type": "int_parsing"}
    )
    assert out == {
        "field": "limit",
        "message": "Input should be a valid integer",
        "type": "int_parsing",
    }
