"""A page that is genuinely mostly charts is THIN and a perfectly good screenshot.

Confirmed live against dashboard.tremor.so: 180 words, nav_shell every time,
six real tier escalations (two of them browser-class) spent chasing text
nobody asked for, and the screenshot the caller actually wanted was never
even attempted — it is fetched only AFTER the THIN check passes. This is a
full ScrapeService integration test, not a unit test of is_nav_shell(), so
it proves the fix at the layer that actually failed live.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from engine.core.fetch.base import FetchRequest, FetchResult
from engine.core.models import ScrapeOptions, Tier
from engine.core.scrape_service import ScrapeService

# Many short, low-prose lines — a dashboard's own nav/label shape, not an
# article. Extracts to exactly the kind of markdown is_nav_shell() rejects.
_LABELS = [
    "Overview",
    "Revenue",
    "Active Users",
    "Conversion Rate",
    "Churn",
    "MRR",
    "ARR",
    "Signups",
    "Sessions",
    "Bounce Rate",
    "Avg Duration",
    "Top Pages",
    "Devices",
    "Locations",
    "Referrers",
    "Campaigns",
    "Goals",
    "Funnels",
    "Cohorts",
    "Retention",
]
DASHBOARD_PAGE = (
    "<html><head><title>Dashboard</title></head><body><nav>"
    + "".join(f"<div class='kpi'><span>{label}</span></div>" for label in _LABELS)
    + "</nav></body></html>"
).encode()


@dataclass
class _ChartHeavyDashboard:
    """A fetcher that also takes pictures, like the browser rung does."""

    async def fetch(self, req: FetchRequest) -> FetchResult:
        return FetchResult(
            url=req.url,
            status_code=200,
            headers={"content-type": "text/html"},
            body=DASHBOARD_PAGE,
            content_type="text/html",
            tier="browser",
            latency_ms=40,
            bytes_transferred=len(DASHBOARD_PAGE),
        )

    async def screenshot(
        self, req: FetchRequest, full_page: bool = False, quality: int | None = None
    ) -> str:
        return "data:image/png;base64,AAAA"

    async def healthcheck(self) -> bool:
        return True


async def test_a_screenshot_only_request_succeeds_on_a_page_that_is_thin_as_text() -> None:
    service = ScrapeService({Tier.BROWSER: _ChartHeavyDashboard()}, persist=False)
    options = ScrapeOptions.model_validate(
        {"formats": [{"type": "screenshot"}], "maxAge": 0, "tier": "browser"}
    )
    outcome = await service.scrape("https://example.com/dashboard", options)

    assert outcome.data.screenshot is not None
    assert outcome.data.screenshot.startswith("data:image/png")


async def test_a_markdown_request_on_the_same_page_still_fails_as_thin() -> None:
    """The fix must not weaken THIN detection for a caller who DOES want text
    — only skip it when nothing text-dependent was asked for."""
    from engine.core.errors import ExtractionFailed

    service = ScrapeService({Tier.BROWSER: _ChartHeavyDashboard()}, persist=False)
    options = ScrapeOptions.model_validate(
        {"formats": ["markdown"], "maxAge": 0, "tier": "browser"}
    )

    with pytest.raises(ExtractionFailed):
        await service.scrape("https://example.com/dashboard", options)


async def test_screenshot_plus_markdown_together_still_fails_as_thin() -> None:
    """Asking for BOTH means text still matters — salvaging here would hand
    back a screenshot next to a markdown field the caller has no way to know
    is empty because it failed, not because the page has none."""
    from engine.core.errors import ExtractionFailed

    service = ScrapeService({Tier.BROWSER: _ChartHeavyDashboard()}, persist=False)
    options = ScrapeOptions.model_validate(
        {"formats": ["markdown", {"type": "screenshot"}], "maxAge": 0, "tier": "browser"}
    )

    with pytest.raises(ExtractionFailed):
        await service.scrape("https://example.com/dashboard", options)
