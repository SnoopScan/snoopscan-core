"""Every /v1 route is reachable by SOME scope.

The scope gate denies a path it does not know, on purpose — failing open is how
the first version enforced nothing. The cost of failing closed is that a new
route is unreachable until it is named, and nothing said so: /v1/serp shipped
with its tests green and answered FORBIDDEN_SCOPE '__unmapped__' to every key
the first time it was called live (Sep 2026). This makes the omission a test
failure instead of a customer's error message.
"""

from __future__ import annotations

from engine.api import scopes
from engine.api.app import app


def test_every_v1_route_is_named_in_the_scope_table() -> None:
    # From the OpenAPI schema, not app.routes: included routers are wrapped
    # lazily, and a walk over app.routes found no /v1 path at all — which made
    # the first draft of this test pass with the table deliberately broken.
    paths = [p for p in app.openapi().get("paths", {}) if p.startswith("/v1/")]
    assert paths, "no /v1 routes found; this check would pass vacuously"
    unmapped = sorted(p for p in paths if scopes.required_for(p) == scopes._UNKNOWN)
    assert unmapped == [], f"routes no key can reach: {unmapped}"
