"""A missing rung must be visible, not inferred from failures.

Camoufox's browser build lives in `~/Library/Caches/camoufox`. A cache cleaner
deleted it. `installed()` correctly returned False, the engine dropped
`stealth_hard` and `mobile` with a single info line, and then answered BLOCKED
for every DataDome domain for an hour — which reads as the targets refusing us
rather than as us having lost the only two rungs that pass them (7 Sep 2026).

The rungs are optional by design (Camoufox is a ~200 MB download a self-hoster
never needs), so their absence is not an error. It is a CAPABILITY CHANGE, and
the health endpoint is where a capability belongs.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.api.app import app


def _health() -> dict[str, Any]:
    with TestClient(app) as client:
        return client.get("/health").json()


def test_health_lists_the_tiers_that_exist() -> None:
    body = _health()
    assert "tiers" in body, "the field an operator needs to see a lost rung"
    assert "http" in body["tiers"], "the cheapest rung is always present"


def test_health_flags_whether_the_deep_rungs_are_available() -> None:
    """A boolean an alert can watch, so nobody has to read a tier list."""
    body = _health()
    assert "deepTiersAvailable" in body
    assert body["deepTiersAvailable"] == ("stealth_hard" in body["tiers"])


def test_the_flag_goes_false_when_camoufox_is_gone(monkeypatch: Any) -> None:
    """The exact failure: package present, browser build deleted.

    Skipped in the open core, where the Camoufox rung is withheld: there is no
    module to patch, and a test that cannot run must say so rather than fail.
    """
    pytest.importorskip("engine.core.fetch.tier3h_camoufox")

    from engine.api import deps
    from engine.core.models import Tier

    monkeypatch.setattr(deps, "_fetchers", None)
    monkeypatch.setattr(
        "engine.core.fetch.tier3h_camoufox.CamoufoxFetcher.installed",
        classmethod(lambda cls: False),
    )
    try:
        body = _health()
        assert body["deepTiersAvailable"] is False
        assert "stealth_hard" not in body["tiers"]
        assert Tier.HTTP.value in body["tiers"], "the cheap rungs still serve"
    finally:
        deps._fetchers = None
