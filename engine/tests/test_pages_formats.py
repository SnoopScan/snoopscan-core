"""A crawl or batch hands back the formats it was asked for.

`/pages` returned `markdown` and nothing else. A job submitted with
`scrapeOptions.formats = ["html", "links"]` had that work done, stored in the
page row, and billed — and then not returned, with no error to say so. It
surfaced as a JSON API fetched through a batch arriving under `markdown`,
because `markdown` was the only field on offer (6 Sep 2026).

Both surfaces read the same mapper now, so `/v1/scrape` and `/pages` cannot
answer the same question differently.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.api.routes import crawl as crawl_routes
from engine.core.models import ScrapeOptions


def _row(**over: Any) -> dict[str, Any]:
    row = {
        "id": "page_1",
        "url": "https://example.com/a",
        "source_url": "https://example.com/a",
        "ok": True,
        "error_code": None,
        "markdown": "# Title\n\nBody.",
        "html": "<article><h1>Title</h1></article>",
        "raw_html": '{"id":1,"name":"thing"}',
        "links": ["https://example.com/b"],
        "status_code": 200,
        "content_type": "text/html",
        "page_type": "article",
        "word_count": 2,
        "extraction_confidence": 0.9,
        "title": "Title",
        "description": None,
        "language": None,
        "author": None,
        "published_at": None,
        "fetched_at": None,
    }
    row.update(over)
    return row


@pytest.mark.parametrize(
    "formats,expected",
    [
        (["markdown"], {"markdown"}),
        (["html"], {"html"}),
        (["rawHtml"], {"rawHtml"}),
        (["links"], {"links"}),
        (["markdown", "html", "rawHtml", "links"], {"markdown", "html", "rawHtml", "links"}),
    ],
)
def test_a_page_carries_every_format_that_was_requested(
    formats: list[str], expected: set[str]
) -> None:
    payload = crawl_routes._page_payload(_row(), ScrapeOptions(formats=formats))
    present = {f for f in ("markdown", "html", "rawHtml", "links") if f in payload}
    assert present == expected


def test_a_format_that_was_not_requested_is_absent_not_null() -> None:
    """Present-and-null reads as "we tried and got nothing"; absent reads as
    "you did not ask". /v1/scrape omits, so /pages omits."""
    payload = crawl_routes._page_payload(_row(), ScrapeOptions(formats=["markdown"]))
    assert "html" not in payload
    assert "rawHtml" not in payload


def test_a_stored_null_is_omitted_even_when_requested() -> None:
    """A format asked for but genuinely empty is left out rather than sent as
    null — the metadata already says what happened to the page."""
    payload = crawl_routes._page_payload(_row(html=None), ScrapeOptions(formats=["html"]))
    assert "html" not in payload


def test_the_identity_fields_are_always_present() -> None:
    """Whatever the formats, a caller can still identify and page the row."""
    payload = crawl_routes._page_payload(_row(), ScrapeOptions(formats=["links"]))
    for key in ("id", "url", "sourceURL", "ok", "errorCode", "metadata"):
        assert key in payload


# --------------------------------------------------------------------------
# The options come off the job, not from a default
# --------------------------------------------------------------------------


def test_options_are_read_from_the_jobs_own_request() -> None:
    job = {"input": {"urls": ["https://example.com"], "scrapeOptions": {"formats": ["html"]}}}
    assert crawl_routes._requested_options(job)._has_format("html")


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"scrapeOptions": None},
        {"scrapeOptions": "nonsense"},
        {"scrapeOptions": {"formats": "bad"}},
    ],
)
def test_an_unreadable_job_input_falls_back_to_defaults(payload: Any) -> None:
    """A page that was fetched perfectly well must still be readable even if the
    stored request cannot be parsed — the pages are the valuable part."""
    options = crawl_routes._requested_options({"input": payload})
    assert isinstance(options, ScrapeOptions)
    assert options._has_format("markdown"), "the default format must still work"


# --------------------------------------------------------------------------
# A cached row must be able to ANSWER the request, not merely match its key
# --------------------------------------------------------------------------


def test_a_cached_row_missing_a_requested_format_is_not_a_hit() -> None:
    """`formats` is deliberately not in the cache key, on the grounds that a row
    holds every format together. It does not — `html` and `raw_html` are stored
    only when the STORING caller asked for them.

    So a markdown-only scrape leaves a row the key says is valid for an `html`
    request and that cannot answer one. Serving it returns an empty `html` with
    `cached: true`: the silent under-delivery the variant hash exists to
    prevent, one level further down (6 Sep 2026).
    """
    from engine.core.scrape_service import _row_satisfies

    thin = _row(html=None, raw_html=None)  # what a markdown-only scrape stores
    assert _row_satisfies(thin, ScrapeOptions(formats=["markdown"]))
    assert not _row_satisfies(thin, ScrapeOptions(formats=["html"]))
    assert not _row_satisfies(thin, ScrapeOptions(formats=["rawHtml"]))
    assert not _row_satisfies(thin, ScrapeOptions(formats=["markdown", "html"]))


def test_a_full_row_satisfies_every_backed_format() -> None:
    """The hit rate must not collapse: whenever the columns are present the row
    is still served, which is the ordinary case."""
    from engine.core.scrape_service import _row_satisfies

    assert _row_satisfies(_row(), ScrapeOptions(formats=["markdown", "html", "rawHtml", "links"]))


def test_a_json_format_does_not_block_a_hit() -> None:
    """`json` is not a content column on the page row; it must not make every
    cached row look unusable."""
    from engine.core.scrape_service import _row_satisfies

    assert _row_satisfies(
        _row(),
        ScrapeOptions(formats=["markdown", {"type": "json", "schema": {"type": "object"}}]),
    )


def test_a_screenshot_request_is_not_satisfied_by_a_row_without_one() -> None:
    """CHANGED 8 Sep 2026 — this asserted the opposite.

    `screenshot` was treated as "not a stored column", so a row with no
    screenshot answered a request for one: the caller got no image, a
    `cached: true`, and a browser-tier bill. The screenshot IS stored
    (`screenshot_path`), so it can be checked like any other.
    """
    from engine.core.scrape_service import _row_satisfies

    assert not _row_satisfies(_row(), ScrapeOptions(formats=["markdown", "screenshot"]))
    assert _row_satisfies(
        _row(screenshot_path="/shots/a.png", fetch_tier="browser"),
        ScrapeOptions(formats=["markdown", "screenshot"]),
    )
