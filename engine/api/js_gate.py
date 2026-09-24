"""The executeJavascript gate, enforced across every route that can carry it.

Arbitrary script execution in a browser context is the largest security
surface the API has (11-compliance.md section 5). It is never exposed over
MCP, and over REST it requires an explicit per-key flag that defaults to
false.

The gate was originally a check inside the `/v1/scrape` handler, which was
wrong in a way that is easy to miss: `actions` is defined on `ScrapeOptions`,
not on `ScrapeRequest`, and FOUR other request models embed a `scrapeOptions`
object — crawl, batch scrape, extract and search. All four accepted
`executeJavascript` from a key whose `allow_js_exec` was false. The guard was
not weak; it was simply not on the other doors.

So the check does not name a field path. It walks the request model and finds
every action wherever it is nested, which means a new route, or a new place
`ScrapeOptions` gets embedded, is covered without anyone remembering to think
about it. `test_js_gate.py` enumerates the routes from the running app and
fails if one accepts a script, so a future unguarded route is a failing test
rather than an incident.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

from engine.core.errors import InvalidRequest

# The action this gate exists for.
GATED_ACTION = "executeJavascript"

# Requests are shallow; this only needs to be deep enough to reach a nested
# `scrapeOptions.actions[]`. A bound also stops a pathological payload turning
# the guard into the slow part of the request.
MAX_DEPTH = 6


def _walk_actions(value: Any, depth: int = 0) -> Iterator[Any]:
    """Yield every action object anywhere in the request tree."""
    if depth > MAX_DEPTH:
        return
    if isinstance(value, BaseModel):
        # An action is identified by carrying a `type` that names one, not by
        # the name of the field holding it — a field name is exactly the thing
        # that varies between routes.
        if isinstance(getattr(value, "type", None), str):
            yield value
        for name in type(value).model_fields:
            yield from _walk_actions(getattr(value, name, None), depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_actions(item, depth + 1)


def guard_js_execution(body: BaseModel, allowed: bool) -> None:
    """Reject a request carrying executeJavascript unless the key permits it.

    Call this on EVERY authenticated route that accepts a request body. It is
    cheap on requests with no actions, and being called where it is not needed
    costs nothing next to being absent where it is.
    """
    if allowed:
        return
    for action in _walk_actions(body):
        if getattr(action, "type", None) == GATED_ACTION:
            raise InvalidRequest(
                f"{GATED_ACTION} is not enabled for this API key",
                {"action": GATED_ACTION},
            )


def carries_js_execution(body: BaseModel) -> bool:
    """True if the request contains a script action. Used by the route test."""
    return any(getattr(a, "type", None) == GATED_ACTION for a in _walk_actions(body))
