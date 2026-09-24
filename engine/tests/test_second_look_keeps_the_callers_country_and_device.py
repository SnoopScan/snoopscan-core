"""The screenshot is a second page load, and it must be the SAME visit.

`screenshot` loads the page again on the browser rung, and the request for that
second load was built from scratch: `mobile=False`, no location, no proxy. So a
caller asking for the German phone view got the page as a desktop browser saw
it from our own server — every time, with nothing in the response to say so.
For ad verification that is the entire product ("screenshot the ad slot from
the US, Germany and Japan, mobile and desktop") returning the wrong picture.

The invariant these pin: the second look is derived from the first request, so
country, device and exit cannot drift apart again.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.core.fetch.base import FetchRequest, FetchResult
from engine.core.models import ScrapeOptions, Tier
from engine.core.scrape_service import ScrapeService

PAGE = (
    b"<html><head><title>Landing</title></head><body><main><h1>Spring sale</h1>"
    + b"<p>Real landing page copy for the campaign, long enough to read as content.</p>" * 40
    + b"</main></body></html>"
)


@dataclass
class _RecordingBrowser:
    """Serves one page and remembers every request it was handed."""

    fetched: list[FetchRequest] = field(default_factory=list)
    shot: list[FetchRequest] = field(default_factory=list)

    async def fetch(self, req: FetchRequest) -> FetchResult:
        self.fetched.append(req)
        return FetchResult(
            url=req.url,
            status_code=200,
            headers={"content-type": "text/html"},
            body=PAGE,
            content_type="text/html",
            tier="browser",
            latency_ms=40,
            bytes_transferred=len(PAGE),
        )

    async def screenshot(
        self, req: FetchRequest, full_page: bool = False, quality: int | None = None
    ) -> str:
        self.shot.append(req)
        return "data:image/png;base64,AAAA"

    async def healthcheck(self) -> bool:
        return True


async def _shoot(options: dict) -> _RecordingBrowser:
    browser = _RecordingBrowser()
    service = ScrapeService({Tier.BROWSER: browser}, persist=False)
    opts = ScrapeOptions.model_validate(
        {"formats": ["markdown", {"type": "screenshot"}], "maxAge": 0, "tier": "browser", **options}
    )
    await service.scrape("https://example.com/spring", opts)
    assert browser.shot, "a screenshot was asked for and never taken"
    return browser


async def test_the_screenshot_is_taken_as_a_phone_when_a_phone_was_asked_for() -> None:
    browser = await _shoot({"mobile": True})
    assert browser.shot[0].mobile is True


async def test_the_screenshot_is_taken_from_the_country_that_was_asked_for() -> None:
    browser = await _shoot({"location": {"country": "DE"}})
    location = browser.shot[0].location
    assert location is not None
    assert location.country.lower() == "de"


async def test_the_screenshot_goes_out_through_the_same_exit_as_the_page() -> None:
    """Whatever exit the page used — none, or a German residential one — the
    picture of it must use the same, or it is a picture of a different visit."""
    browser = await _shoot({"location": {"country": "DE"}, "mobile": True})
    first, second = browser.fetched[0], browser.shot[0]
    assert second.proxy_url == first.proxy_url
    assert second.proxy_id == first.proxy_id
    assert second.headers == first.headers, "consent cookies and caller headers carry over"


async def test_the_second_look_still_loads_the_assets() -> None:
    """A picture without its images is not a picture of the page."""
    browser = await _shoot({"blockAssets": True})
    assert browser.shot[0].block_assets is False
