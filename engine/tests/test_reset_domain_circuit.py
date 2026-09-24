"""An operator can close one domain's circuit breaker without touching the DB.

The breaker's backoff doubles on each opening with no success between, which
is right for a site that is genuinely down and wrong once the cause was ours:
a fix for why a domain failed left g2.com locked for hours after it shipped
(Sep 2026), and the only way to clear it was an UPDATE typed into production.
Clearing a learned lockout is an operator action; it belongs on the internal
API where the desk can reach it, with the rest of the profile left alone.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine import settings as settings_mod
from engine.api.app import app
from engine.storage import repositories as repo

TOKEN = "test-internal-token"
H = {"X-Internal-Token": TOKEN}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings_mod.get_settings(), "internal_token", TOKEN)
    yield


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_the_breaker_is_cleared_for_that_domain(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleared: list[str] = []

    async def reset(domain: str) -> bool:
        cleared.append(domain)
        return True

    monkeypatch.setattr(repo, "reset_domain_circuit", reset)
    r = client.post("/internal/domains/g2.com/reset-circuit", headers=H)
    assert r.status_code == 200, r.text
    assert cleared == ["g2.com"]


def test_an_unknown_domain_is_a_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def reset(domain: str) -> bool:
        return False

    monkeypatch.setattr(repo, "reset_domain_circuit", reset)
    assert client.post("/internal/domains/nope.example/reset-circuit", headers=H).status_code == 404


def test_it_needs_the_internal_token(client: TestClient) -> None:
    assert client.post("/internal/domains/g2.com/reset-circuit").status_code in (401, 403)


def test_the_repository_touches_only_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """What was LEARNED about the domain — its WAF, its floor, its country —
    stays. Only the lockout and its backoff counter go."""
    import asyncio

    seen: dict[str, Any] = {}

    async def execute(query: str, *args: Any) -> str:
        seen["query"], seen["args"] = " ".join(query.split()), args
        return "UPDATE 1"

    monkeypatch.setattr(repo.db, "execute", execute)
    assert asyncio.run(repo.reset_domain_circuit("g2.com")) is True
    q = seen["query"]
    assert "circuit_open_until = NULL" in q and "circuit_opens = 0" in q
    for learned in ("detected_waf", "min_working_tier", "working_country", "block_count"):
        assert learned not in q
    assert seen["args"] == ("g2.com",)
