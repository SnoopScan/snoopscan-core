"""Scopes are enforced, and enforced at the chokepoint.

They were stored on every key, offered as four checkboxes in the customer's
dashboard, and checked nowhere — a control that grants nothing and withholds
nothing, which is worse than no control because it tells a customer they have
limited a key's blast radius when they have not.

Measured 9 Sep 2026: Typesense scopes by endpoint, Algolia by ACL, Stripe by
restricted key and recommends against unrestricted ones. Firecrawl does not
scope at all — and does not offer the control either.
"""

from __future__ import annotations

import pytest

from engine.api.scopes import ALL_SCOPES, SCOPE_BY_SEGMENT, required_for


def test_every_billable_route_needs_a_scope() -> None:
    """The routes as the OpenAPI schema lists them. One that needs no scope is
    one anybody's key can call."""
    for path in [
        "/v1/scrape",
        "/v1/crawl",
        "/v1/map",
        "/v1/search",
        "/v1/extract",
        "/v1/batch/scrape",
        "/v1/products",
        "/v1/posts",
        "/v1/monitor",
        "/v1/places/search",
        "/v1/parse",
    ]:
        scope = required_for(path)
        assert scope in ALL_SCOPES, f"{path} is not scoped: {scope!r}"


def test_reading_a_job_back_rides_on_having_started_it() -> None:
    for path, expected in [
        ("/v1/crawl/abc123", "crawl"),
        ("/v1/crawl/abc123/pages", "crawl"),
        ("/v1/crawl/abc123/errors", "crawl"),
        ("/v1/batch/abc123", "batch"),
        ("/v1/batch/abc123/pages", "batch"),
        ("/v1/monitor/m1/checks", "monitor"),
        ("/v1/monitor/m1/run", "monitor"),
    ]:
        assert required_for(path) == expected, path


def test_an_unmapped_v1_route_denies_rather_than_opens() -> None:
    """The whole failure mode, inverted. A new endpoint added without naming
    it here is unreachable — loudly — instead of being callable by every key,
    which is how the first version enforced nothing at all."""
    scope = required_for("/v1/something-new")

    assert scope is not None
    assert scope not in ALL_SCOPES, "an unmapped route resolved to a real scope"


def test_routes_outside_v1_are_not_scoped_here() -> None:
    """Health, docs and the MCP endpoint have their own gates."""
    for path in ["/health", "/", "/mcp", "/internal/keys", "/docs"]:
        assert required_for(path) is None, path


def test_the_scope_list_is_exactly_what_the_map_grants() -> None:
    """A scope offered in the dashboard that unlocks nothing, or a segment
    needing a scope nobody can hold, are both dead controls."""
    assert set(ALL_SCOPES) == set(SCOPE_BY_SEGMENT.values())


@pytest.mark.parametrize("segment", sorted(SCOPE_BY_SEGMENT))
def test_every_mapped_segment_resolves(segment: str) -> None:
    assert required_for(f"/v1/{segment}") == SCOPE_BY_SEGMENT[segment]


def test_the_check_lives_in_the_dependency_every_route_shares() -> None:
    """Not at the callers. A scope check added per route is one somebody
    forgets on the next endpoint — which is the class of bug this fixes."""
    import inspect

    from engine.api import deps

    src = inspect.getsource(deps.require_api_key)
    assert "required_for" in src, "the scope check has left the chokepoint"
    assert "ForbiddenScope" in src
