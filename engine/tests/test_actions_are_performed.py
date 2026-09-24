"""Actions must actually happen, and the caller must hear when they do not.

`actions` shipped in the request model, forced the browser tier, set a sticky
proxy — and was then dropped on the floor. A customer's click sequence came
back 200, billed at browser rates, with none of the steps performed and
nothing in the answer saying so. These tests OBSERVE the calls rather than
asserting the request was accepted.
"""

from __future__ import annotations

# The fake page mirrors Playwright's signatures, `timeout` included: this
# stands in for an API we call, not one we are designing.
# ruff: noqa: ASYNC109
import contextlib
from typing import Any

import pytest

from engine.core.fetch.actions import ActionError, Results
from engine.core.fetch.actions import run as run_actions
from engine.core.models import ScrapeOptions
from engine.core.scrape_service import ScrapeService
from engine.tests.fixtures.builders import article_html


@pytest.fixture(autouse=True)
def _offline_target_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """These fake-fetcher tests must not depend on external DNS."""
    from unittest.mock import AsyncMock

    import tldextract

    from engine.core.politeness import PolitenessDecision
    from engine.core.ssrf import ResolvedTarget

    monkeypatch.setattr(
        "engine.core.urls._extract", tldextract.TLDExtract(cache_dir=None, suffix_list_urls=())
    )

    async def resolved(url: str) -> ResolvedTarget:
        return ResolvedTarget(
            url=url, host="example.com", port=443, scheme="https", addresses=("93.184.216.34",)
        )

    monkeypatch.setattr("engine.core.scrape_service.resolve_and_validate", resolved)
    monkeypatch.setattr(
        "engine.core.politeness.PolitenessGate.acquire",
        AsyncMock(return_value=PolitenessDecision(allowed=True)),
    )
    monkeypatch.setattr("engine.core.politeness.PolitenessGate.release", AsyncMock())


class FakeMouse:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self._log = log

    async def wheel(self, dx: float, dy: float) -> None:
        self._log.append(("wheel", (dx, dy)))


class FakeKeyboard:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self._log = log

    async def press(self, key: str) -> None:
        self._log.append(("press", key))


class FakePage:
    """Records every call, so a test can see the click happened."""

    def __init__(self, missing: str | None = None) -> None:
        self.log: list[tuple[str, Any]] = []
        self.missing = missing  # a selector that does not exist
        self.url = "https://example.com/after"
        self.mouse = FakeMouse(self.log)
        self.keyboard = FakeKeyboard(self.log)

    async def click(self, selector: str, *, timeout: float) -> None:
        if selector == self.missing:
            raise TimeoutError(f"no element matches {selector}")
        self.log.append(("click", selector))

    async def fill(self, selector: str, value: str, *, timeout: float) -> None:
        self.log.append(("fill", (selector, value)))

    async def wait_for_selector(self, selector: str, *, timeout: float) -> None:
        if selector == self.missing:
            raise TimeoutError(f"never appeared: {selector}")
        self.log.append(("wait_selector", selector))

    async def wait_for_timeout(self, ms: float) -> None:
        self.log.append(("wait_ms", ms))

    async def screenshot(self, **kw: Any) -> bytes:
        self.log.append(("screenshot", kw.get("full_page")))
        return b"\xff\xd8jpeg-bytes"

    async def content(self) -> str:
        self.log.append(("content", None))
        return "<html><body>after the click</body></html>"

    async def evaluate(self, script: str) -> Any:
        self.log.append(("evaluate", script))
        return {"title": "after", "count": 3}


def steps(*raw: dict[str, Any]) -> list[Any]:
    """Validated Action models, exactly as a request would carry them."""
    return ScrapeOptions.model_validate({"actions": list(raw)}).actions


async def test_every_step_actually_reaches_the_page() -> None:
    page = FakePage()
    await run_actions(
        page,
        steps(
            {"type": "click", "selector": "#accept"},
            {"type": "write", "selector": "#q", "text": "hello"},
            {"type": "press", "key": "Enter"},
            {"type": "scroll", "direction": "down", "amount": 2},
            {"type": "wait", "selector": "#results"},
        ),
        timeout_ms=30_000,
    )

    kinds = [k for k, _ in page.log]
    assert ("click", "#accept") in page.log, "the click never happened"
    assert ("fill", ("#q", "hello")) in page.log
    assert ("press", "Enter") in page.log
    assert ("wait_selector", "#results") in page.log
    assert "wheel" in kinds, "the scroll never happened"
    wheel = next(v for k, v in page.log if k == "wheel")
    assert wheel[1] > 0, "scrolling down must move the page down"


async def test_scrolling_up_goes_up() -> None:
    page = FakePage()
    await run_actions(page, steps({"type": "scroll", "direction": "up"}), timeout_ms=10_000)
    wheel = next(v for k, v in page.log if k == "wheel")
    assert wheel[1] < 0


async def test_a_selector_that_does_not_match_is_the_callers_error() -> None:
    """Named by index and type. "The scrape failed" sends someone looking at
    the site for a fault that is in their own selector."""
    page = FakePage(missing="#nope")
    with pytest.raises(ActionError) as caught:
        await run_actions(
            page,
            steps({"type": "click", "selector": "#fine"}, {"type": "click", "selector": "#nope"}),
            timeout_ms=10_000,
        )
    assert caught.value.index == 1, "it must say WHICH step"
    assert caught.value.kind == "click"
    assert "#fine" in str(page.log), "the step before it still ran"


async def test_the_sequence_stops_at_the_first_failure() -> None:
    page = FakePage(missing="#nope")
    with pytest.raises(ActionError):
        await run_actions(
            page,
            steps(
                {"type": "click", "selector": "#nope"},
                {"type": "click", "selector": "#never-reached"},
            ),
            timeout_ms=10_000,
        )
    assert ("click", "#never-reached") not in page.log


async def test_screenshots_scrapes_and_js_come_back_to_the_caller() -> None:
    page = FakePage()
    out = await run_actions(
        page,
        steps(
            {"type": "screenshot", "fullPage": True},
            {"type": "scrape"},
            {"type": "executeJavascript", "script": "document.title"},
        ),
        timeout_ms=30_000,
    )
    assert len(out.screenshots) == 1
    assert out.screenshots[0].startswith("data:image/jpeg;base64,")
    assert out.scrapes[0]["html"] == "<html><body>after the click</body></html>"
    assert out.scrapes[0]["url"] == "https://example.com/after"
    assert out.javascript_returns[0]["value"] == {"title": "after", "count": 3}
    assert out.as_dict()["javascriptReturns"], "the response shape the API declares"


async def test_a_step_cannot_outlive_the_requests_own_budget() -> None:
    """A selector that never appears must not hold a browser slot open until
    the whole request dies."""
    page = FakePage()
    # A single millisecond of budget: there is nothing left for step 0.
    with pytest.raises(ActionError) as caught:
        await run_actions(page, steps({"type": "click", "selector": "#x"}), timeout_ms=1)
    assert "ran out of time" in caught.value.detail


async def test_a_page_the_browser_cannot_serialise_does_not_500() -> None:
    """evaluate() can hand back a DOM node or a function. Saying so beats the
    response encoder falling over."""

    class Weird(FakePage):
        async def evaluate(self, script: str) -> Any:
            return {"node": object()}

    out = await run_actions(
        Weird(), steps({"type": "executeJavascript", "script": "x"}), timeout_ms=10_000
    )
    assert isinstance(out.javascript_returns[0]["value"]["node"], str)


async def test_no_actions_is_not_an_empty_result_object() -> None:
    """A plain scrape must not grow an empty `actions` block in its answer."""
    assert not Results()


# --------------------------------------------------------------------------
# The cache, which would undo all of the above
# --------------------------------------------------------------------------


def variant(**kw: Any) -> tuple[bytes, bool]:
    return ScrapeService._cache_variant("https://example.com/p", ScrapeOptions.model_validate(kw))


def test_two_different_click_sequences_are_not_the_same_cached_page() -> None:
    a, _ = variant(actions=[{"type": "click", "selector": "#a"}])
    b, _ = variant(actions=[{"type": "click", "selector": "#b"}])
    assert a != b, "clicking #a and clicking #b shared a cache key"


def test_a_page_reached_by_clicking_is_not_the_page_at_the_url() -> None:
    plain, _ = variant()
    clicked, _ = variant(actions=[{"type": "click", "selector": "#a"}])
    assert plain != clicked, "a request carrying steps accepted a page where none had run"


def test_an_interaction_never_enters_the_shared_cache() -> None:
    """It is a side effect, and very often personalised — a form filled, a tab
    opened while signed in. One caller's post-click page must not become
    another caller's answer."""
    _, shareable = variant(actions=[{"type": "click", "selector": "#a"}])
    assert shareable is False
    _, plain_shareable = variant()
    assert plain_shareable is True, "and an ordinary scrape still caches"


# --------------------------------------------------------------------------
# End to end: the steps have to LEAVE the service
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        {"type": "click", "selector": "#accept"},
        {"type": "captchaCheckbox", "selector": "#accept", "successSelector": "#results"},
    ],
)
async def test_a_scrape_request_carries_its_steps_all_the_way_to_the_fetcher(
    action: dict[str, Any],
) -> None:
    """The original fault, pinned at the seam it went missing at. `actions`
    reached the service, forced the browser tier, chose a sticky proxy — and
    was never put on the FetchRequest, so the fetcher could not have run it
    even if it had wanted to."""
    from engine.core.fetch.base import FetchRequest, FetchResult
    from engine.core.models import Tier

    seen: list[FetchRequest] = []

    class Recorder:
        name = "browser"

        async def fetch(self, req: FetchRequest) -> FetchResult:
            seen.append(req)
            body = article_html().encode("utf-8")
            return FetchResult(
                url=req.url,
                status_code=200,
                headers={},
                body=body,
                content_type="text/html; charset=utf-8",
                tier=self.name,
                latency_ms=10,
                bytes_transferred=len(body),
                action_results={
                    "screenshots": [],
                    "scrapes": [{"url": req.url, "html": "<p>after</p>"}],
                    "javascriptReturns": [],
                    "captcha": [{"status": "content_visible", "clicked": True}],
                },
            )

        async def healthcheck(self) -> bool:
            return True

    service = ScrapeService({Tier.BROWSER: Recorder()}, persist=False)
    outcome = await service.scrape(
        "https://example.com/p",
        ScrapeOptions.model_validate({"actions": [action], "formats": ["markdown"]}),
    )

    assert seen, "no fetch was made"
    assert seen[0].actions, "the steps never reached the fetcher"
    assert seen[0].actions[0].selector == "#accept"
    # ...and what they produced comes back in the answer, not just in a log.
    assert outcome.data.actions is not None
    assert outcome.data.actions.scrapes[0]["html"] == "<p>after</p>"
    assert outcome.data.actions.captcha[0]["status"] == "content_visible"


async def test_a_plain_scrape_still_reports_no_actions() -> None:
    from engine.core.fetch.base import FetchRequest, FetchResult
    from engine.core.models import Tier

    class Plain:
        name = "http"

        async def fetch(self, req: FetchRequest) -> FetchResult:
            body = article_html().encode("utf-8")
            return FetchResult(
                url=req.url, status_code=200, headers={}, body=body,
                content_type="text/html; charset=utf-8", tier=self.name,
                latency_ms=10, bytes_transferred=len(body),
            )  # fmt: skip

        async def healthcheck(self) -> bool:
            return True

    service = ScrapeService({Tier.HTTP: Plain()}, persist=False)
    outcome = await service.scrape("https://example.com/p", ScrapeOptions())
    assert outcome.data.actions is None


async def test_a_bad_selector_is_not_retried_and_is_not_called_a_network_fault() -> None:
    """Measured live: a mistyped selector came back FETCH_FAILED,
    "Network-level failure fetching the target", after being attempted on the
    browser tier TWICE — so a typo read as a connection problem and billed
    for two browser fetches."""
    from engine.core.errors import InvalidRequest
    from engine.core.fetch.base import FetchRequest, FetchResult
    from engine.core.models import Tier

    calls = {"n": 0}

    class Missing:
        name = "browser"

        async def fetch(self, req: FetchRequest) -> FetchResult:
            calls["n"] += 1
            out = FetchResult(
                url=req.url, status_code=None, headers={}, body=b"",
                content_type=None, tier=self.name, latency_ms=5,
                bytes_transferred=0, error="action 0 (click) failed: TimeoutError",
            )  # fmt: skip
            out.action_error = "action 0 (click) failed: TimeoutError"
            return out

        async def healthcheck(self) -> bool:
            return True

    service = ScrapeService({Tier.BROWSER: Missing(), Tier.STEALTH: Missing()}, persist=False)
    with pytest.raises(InvalidRequest) as caught:
        await service.scrape(
            "https://example.com/p",
            ScrapeOptions.model_validate({"actions": [{"type": "click", "selector": "#nope"}]}),
        )

    assert calls["n"] == 1, f"the caller's typo was attempted {calls['n']} times"
    assert "action 0 (click)" in str(caught.value), "it must still say which step"


# --------------------------------------------------------------------------
# A malformed selector and a selector that timed out are different answers
# --------------------------------------------------------------------------


async def test_a_selector_that_will_not_parse_is_the_callers_to_fix() -> None:
    class BadSyntax(FakePage):
        async def click(self, selector: str, *, timeout: float) -> None:
            raise ValueError('Unexpected token "!!" while parsing selector')

    with pytest.raises(ActionError) as caught:
        await run_actions(
            BadSyntax(), steps({"type": "click", "selector": "div!!"}), timeout_ms=10_000
        )
    assert caught.value.fault == "invalid"


async def test_a_well_formed_selector_that_times_out_is_not_a_syntax_error() -> None:
    """The selector may be right and the page changed, or never finished
    loading, or showed a challenge. Telling them to fix their syntax sends
    them after the wrong thing."""
    page = FakePage(missing="#gone")
    with pytest.raises(ActionError) as caught:
        await run_actions(page, steps({"type": "click", "selector": "#gone"}), timeout_ms=10_000)
    assert caught.value.fault == "timeout"
    assert "did not answer" in caught.value.detail


@pytest.mark.parametrize(
    ("fault", "expected"),
    [("invalid", "INVALID_REQUEST"), ("timeout", "EXTRACTION_FAILED")],
)
def test_each_fault_gets_its_own_answer(fault: str, expected: str) -> None:
    from engine.core.detect.validator import Reason, Verdict
    from engine.core.scrape_service import _error_for

    verdict = Verdict(
        ok=False,
        reason=Reason.BLOCKED,
        signal="action_failed",
        confidence=1.0,
        details={"error": "action 0 (click) failed: ...", "fault": fault},
    )
    assert _error_for(verdict, ["browser"]).code == expected


async def test_the_fetch_log_records_which_exit_carried_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`log_fetch_attempt` has taken proxy_id since it was written and nothing
    passed it, so every row read as direct — 3,300 attempts over 30 days at
    100% direct, including ones known to be proxied. It is the number that
    says how much traffic the direct-path guards actually govern."""
    from engine.core.fetch.base import FetchRequest, FetchResult
    from engine.core.models import Tier

    logged: list[dict[str, Any]] = []

    class Exiting:
        name = "http"

        async def fetch(self, req: FetchRequest) -> FetchResult:
            body = article_html().encode("utf-8")
            return FetchResult(
                url=req.url, status_code=200, headers={}, body=body,
                content_type="text/html; charset=utf-8", tier=self.name,
                latency_ms=10, bytes_transferred=len(body),
                proxy_id="exit-abc", proxy_type="residential",
            )  # fmt: skip

        async def healthcheck(self) -> bool:
            return True

    service = ScrapeService({Tier.HTTP: Exiting()}, persist=True)

    # A persisted scrape reads and writes a dozen tables. Answer every one with
    # "nothing on record" so the test needs no database: it passed only on a
    # machine that had one, and failed in the published suite, which has none.
    from engine.storage import db

    async def nothing(*a: Any, **k: Any) -> Any:
        return None

    async def no_rows(*a: Any, **k: Any) -> list[Any]:
        return []

    for name in ("fetchrow", "fetchval", "execute", "executemany"):
        monkeypatch.setattr(db, name, nothing)
    monkeypatch.setattr(db, "fetch", no_rows)

    async def spy(**kw: Any) -> int:
        logged.append(kw)
        return 1

    import engine.core.scrape_service as svc

    original = svc.repo.log_fetch_attempt
    svc.repo.log_fetch_attempt = spy  # type: ignore[assignment]
    try:
        with contextlib.suppress(Exception):
            await service.scrape("https://example.com/p", ScrapeOptions())
    finally:
        svc.repo.log_fetch_attempt = original  # type: ignore[assignment]

    assert logged, "nothing was logged at all"
    assert logged[0].get("proxy_id") == "exit-abc", "the exit was not recorded"
    assert logged[0].get("url_hash"), "and the url hash is still recorded"
