"""A request that needs a browser must not be served a body no browser produced.

Reported from a real harvest, 8 Sep 2026, and confirmed in the source before
being fixed: `_cache_variant` keys on url, country, mobile and extraction, so a
scrape carrying `actions` or `waitFor` shares a key with one carrying neither.
`_row_satisfies` then checked only the format columns, so the row was served.

The caller asked for five seconds of settling, or for clicks, got a plain HTTP
body that had neither, and was BILLED AT BROWSER RATES for it — the tell was
two results of identical length where one had run a browser and one had not.

Fixed in `_row_satisfies` rather than in the key, deliberately: the reverse
direction is safe. A cheap request may reuse a browser-rendered row, and adding
`forces_browser` to the key would fragment the cache for no gain.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.core.models import ScrapeOptions
from engine.core.scrape_service import _row_satisfies


def _row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "markdown": "# Title\n\nBody.",
        "html": "<h1>Title</h1>",
        "raw_html": "<html></html>",
        "links": [],
        "fetch_tier": "http",
        "screenshot_path": None,
    }
    row.update(over)
    return row


@pytest.mark.parametrize(
    "options",
    [
        ScrapeOptions(waitFor=5000),
        ScrapeOptions(formats=["markdown", "screenshot"]),
    ],
    ids=["waitFor", "screenshot"],
)
def test_a_browser_request_rejects_a_plain_http_row(options: ScrapeOptions) -> None:
    assert options.forces_browser, "the fixture must actually force a browser"
    assert not _row_satisfies(_row(fetch_tier="http"), options)


@pytest.mark.parametrize("tier", ["browser", "stealth", "stealth_hard", "mobile"])
def test_a_browser_request_accepts_a_row_from_any_browser_rung(tier: str) -> None:
    options = ScrapeOptions(waitFor=5000)
    assert _row_satisfies(_row(fetch_tier=tier), options)


def test_a_cheap_request_may_still_reuse_a_browser_row() -> None:
    """The reverse direction is safe and worth keeping — a browser-rendered row
    is strictly richer, and refusing it would fragment the cache for nothing."""
    assert _row_satisfies(_row(fetch_tier="stealth_hard"), ScrapeOptions())


def test_a_plain_request_is_unaffected() -> None:
    assert _row_satisfies(_row(fetch_tier="http"), ScrapeOptions())


def test_a_missing_tier_column_never_raises() -> None:
    """Rows arrive as asyncpg Records and as dicts; an older row must not
    crash the cache check."""
    thin = {"markdown": "x", "html": None, "raw_html": None, "links": []}
    assert not _row_satisfies(thin, ScrapeOptions(waitFor=1000))
    assert _row_satisfies(thin, ScrapeOptions(formats=["markdown"]))
