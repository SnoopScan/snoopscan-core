"""A crawl is quoted and capped BEFORE it is queued.

A queued crawl bills per page as it runs. Until now the only credit gate was
`assert_credits`, which asks whether the balance is above zero — so a caller
with 5 credits could submit `limit: 10000`, have it accepted in full, and
watch it die part-done with nothing said at submission time. The ceiling is
now decided at the door: honoured when it fits, lowered when it does not,
refused when the balance buys no pages at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.api import billing, deps
from engine.api.app import app
from engine.api.routes import crawl as crawl_routes
from engine.core.credits import OPERATOR_OWNER
from engine.settings import settings
from engine.storage import repositories as repo
from engine.storage.repositories import ApiKey


def _key(credits: int, owner: str = "cust") -> ApiKey:
    return ApiKey(
        id="key_c",
        label="c",
        scopes=["scrape", "crawl", "map"],
        rate_limit_rpm=1000,
        allow_js_exec=False,
        webhook_secret=None,
        active=True,
        owner_ref=owner,
        credits_remaining=credits,
    )


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Capture what would have been queued, without a database or a queue."""
    seen: dict[str, Any] = {}

    async def fake_create_job(kind: str, key_id: str, payload: dict[str, Any], **kw: Any) -> str:
        seen["kind"] = kind
        seen["payload"] = payload
        return "crawl_test"

    async def fake_push(*a: Any, **kw: Any) -> None:
        seen["pushed"] = True

    async def fake_costs() -> dict[str, int]:
        return {"direct": 1, "proxied": 2, "browser": 5}

    # The test host does not resolve; SSRF is the fetch suites' concern.
    monkeypatch.setattr(settings, "ssrf_guard_enabled", False)
    monkeypatch.setattr(repo, "create_job", fake_create_job)
    monkeypatch.setattr(crawl_routes._queue, "push", fake_push)
    monkeypatch.setattr(repo, "get_credit_costs", fake_costs)
    billing.invalidate_cost_table()
    yield seen
    app.dependency_overrides.clear()
    billing.invalidate_cost_table()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _start(client: TestClient, **body: Any) -> Any:
    return client.post("/v1/crawl", json={"url": "https://example.com", **body})


def test_a_limit_the_balance_covers_is_honoured_as_asked(
    client: TestClient, queued: dict[str, Any]
) -> None:
    app.dependency_overrides[deps.require_api_key] = lambda: _key(5_000)
    r = _start(client, limit=40)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["limit"] == 40 and data["limitRequested"] == 40
    assert data["creditsPerPage"] == 1 and data["creditsMax"] == 40
    # and the job is queued with the limit the caller asked for
    assert queued["payload"]["limit"] == 40


def test_a_limit_beyond_the_balance_is_lowered_to_what_it_covers(
    client: TestClient, queued: dict[str, Any]
) -> None:
    app.dependency_overrides[deps.require_api_key] = lambda: _key(40)
    r = _start(client, limit=10_000)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["limitRequested"] == 10_000
    assert data["limit"] == 40, "40 credits at 1/page buys 40 pages, not 10,000"
    assert data["creditsMax"] == 40
    # the WORKER must receive the lowered limit, not the requested one —
    # otherwise the cap is cosmetic and the crawl overruns anyway
    assert queued["payload"]["limit"] == 40


def test_an_explicit_proxy_is_priced_as_one(client: TestClient, queued: dict[str, Any]) -> None:
    """`auto` usually goes direct; asking for residential is a guarantee of a proxy."""
    app.dependency_overrides[deps.require_api_key] = lambda: _key(40)
    r = _start(client, limit=10_000, scrapeOptions={"proxy": "residential"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["creditsPerPage"] == 2
    assert data["limit"] == 20, "40 credits at 2/page buys 20 pages"


def test_the_default_auto_proxy_is_quoted_at_the_direct_rate(
    client: TestClient, queued: dict[str, Any]
) -> None:
    """Quoting `auto` as proxied would halve every ordinary crawl's ceiling."""
    app.dependency_overrides[deps.require_api_key] = lambda: _key(40)
    r = _start(client, limit=10_000, scrapeOptions={"proxy": "auto"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["creditsPerPage"] == 1


def test_a_balance_that_buys_no_pages_is_refused_before_anything_is_queued(
    client: TestClient, queued: dict[str, Any]
) -> None:
    app.dependency_overrides[deps.require_api_key] = lambda: _key(0)
    r = _start(client, limit=10)
    assert r.status_code == 402, r.text
    assert r.json()["error"]["code"] == "INSUFFICIENT_CREDITS"
    assert "payload" not in queued, "nothing may be queued when it cannot be paid for"


def test_operator_work_is_never_lowered(client: TestClient, queued: dict[str, Any]) -> None:
    """Metered, but not gated — the same split assert_credits makes."""
    app.dependency_overrides[deps.require_api_key] = lambda: _key(0, owner=OPERATOR_OWNER)
    r = _start(client, limit=5_000)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["limit"] == 5_000
    assert queued["payload"]["limit"] == 5_000


# --------------------------------------------------------------------------
# The arithmetic itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_affordable_limit_floors_rather_than_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """9 credits at 2/page is 4 pages, not 4.5 and not 5."""

    async def fake_costs() -> dict[str, int]:
        return {"direct": 1, "proxied": 2}

    monkeypatch.setattr(repo, "get_credit_costs", fake_costs)
    billing.invalidate_cost_table()
    assert await billing.affordable_limit(_key(9), 100, per_page=2) == 4
    billing.invalidate_cost_table()
