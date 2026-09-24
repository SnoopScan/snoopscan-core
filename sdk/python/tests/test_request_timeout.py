"""A caller's `timeout` (engine ms) must widen this client's own HTTP deadline.

Real bug: `timeout` in a scrape()/crawl()/etc request body is the ENGINE's
own time budget — how long it may keep trying tiers server-side. The SDK's
OWN httpx client had a fixed DEFAULT_TIMEOUT (120s) set once at construction,
oblivious to it. Ask the engine for exactly 120s and the two deadlines raced:
whichever fired first won, sometimes surfacing this client's own
httpx.ReadTimeout instead of the engine's clean JSON error — reported live,
a 120-second scrape() crashed with a raw ReadTimeout that never reached the
engine's own error handling at all.
"""

from __future__ import annotations

from snoopscan.client import _http_timeout_for


def test_a_larger_engine_timeout_widens_the_http_deadline() -> None:
    # 120s engine timeout + 10s margin must exceed a small client default.
    assert _http_timeout_for({"timeout": 120_000}, client_default=60.0) == 130.0


def test_the_client_default_is_never_narrowed() -> None:
    # A caller's SHORT engine timeout must not shrink the client's own floor.
    assert _http_timeout_for({"timeout": 5_000}, client_default=120.0) == 120.0


def test_no_engine_timeout_falls_back_to_the_client_default() -> None:
    assert _http_timeout_for({}, client_default=120.0) == 120.0
    assert _http_timeout_for({"query": "widgets"}, client_default=120.0) == 120.0


def test_a_non_numeric_timeout_is_ignored_rather_than_raising() -> None:
    assert _http_timeout_for({"timeout": "soon"}, client_default=120.0) == 120.0
