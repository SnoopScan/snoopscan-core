"""The operator API: the token gate, key lifecycle, credit grants, and metering.

Repository calls are monkeypatched — no database. Under test is the contract
the app relies on and the gate in front of it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine import settings as settings_mod
from engine.api import billing, deps
from engine.api.app import app
from engine.core.credits import OPERATOR_OWNER
from engine.core.models import Cost, PageMetadata, ScrapeData
from engine.core.scrape_service import ScrapeOutcome
from engine.storage import repositories as repo
from engine.storage.repositories import ApiKey

TOKEN = "test-internal-token"
H = {"X-Internal-Token": TOKEN}


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings_mod.get_settings(), "internal_token", TOKEN)
    billing.invalidate_cost_table()
    yield
    app.dependency_overrides.clear()
    billing.invalidate_cost_table()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _key(credits: int, owner: str | None = "cust", suspended: bool = False) -> ApiKey:
    return ApiKey(
        id="key_c",
        label="c",
        scopes=["scrape"],
        rate_limit_rpm=60,
        allow_js_exec=False,
        webhook_secret=None,
        active=True,
        owner_ref=owner,
        credits_remaining=credits,
        concurrency=5,
        suspended=suspended,
    )


class _Service:
    def __init__(self, cost: Cost) -> None:
        self.cost = cost
        self.calls = 0

    async def scrape(self, url: str, options: Any, **kw: Any) -> ScrapeOutcome:
        self.calls += 1
        return ScrapeOutcome(
            data=ScrapeData(
                markdown="# hi", metadata=PageMetadata(sourceURL=url, url=url), cost=self.cost
            )
        )


def test_gate_refuses_missing_and_wrong_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_health() -> dict[str, Any]:
        return {"database": True}

    monkeypatch.setattr(repo, "health_summary", fake_health)
    assert client.get("/internal/health").status_code == 401
    assert client.get("/internal/health", headers={"X-Internal-Token": "nope"}).status_code == 401
    assert client.get("/internal/health", headers=H).status_code == 200


def test_gate_is_closed_when_no_token_is_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings_mod.get_settings(), "internal_token", "")
    assert client.get("/internal/health", headers={"X-Internal-Token": ""}).status_code == 401


def test_create_key_returns_plaintext_once_and_passes_ownership(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_create(plaintext: str, label: str, **kw: Any) -> str:
        seen.update(kw, plaintext=plaintext, label=label)
        return "key_123"

    monkeypatch.setattr(repo, "create_api_key", fake_create)
    body = {"owner_ref": "cust-uuid", "label": "Prod"}
    r = client.post("/internal/keys", headers=H, json=body)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["id"] == "key_123"
    assert data["key"].startswith("sk_") and data["prefix"] == data["key"][:11]
    assert len(data["webhook_secret"]) >= 32
    assert seen["owner_ref"] == "cust-uuid"


def test_create_key_rejects_unknown_fields(client: TestClient) -> None:
    # The engine maps request validation to 400 INVALID_REQUEST, not 422.
    r = client.post(
        "/internal/keys", headers=H, json={"owner_ref": "x", "label": "L", "credits": 5}
    )
    assert r.status_code == 400


def test_grant_credits_is_per_owner_and_returns_the_new_balance(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_adjust(owner_ref: str, delta: int) -> int:
        assert owner_ref == "cust-uuid"
        return 1500 + delta

    monkeypatch.setattr(repo, "adjust_credits", fake_adjust)
    r = client.post("/internal/owners/cust-uuid/credits", headers=H, json={"delta": 500})
    assert r.status_code == 200 and r.json()["data"]["credits_remaining"] == 2000


def test_suspend_owner_then_their_key_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: dict[str, Any] = {"suspended": False}

    async def fake_update(owner_ref: str, **fields: Any) -> bool:
        state.update(fields)
        return True

    async def fake_get(owner_ref: str) -> dict[str, Any]:
        return {
            "owner_ref": owner_ref,
            "credits_remaining": 10,
            "concurrency": 5,
            **state,
            "updated_at": "x",
        }

    monkeypatch.setattr(repo, "update_owner", fake_update)
    monkeypatch.setattr(repo, "get_owner", fake_get)
    r = client.patch("/internal/owners/cust", headers=H, json={"suspended": True})
    assert r.status_code == 200 and r.json()["data"]["suspended"] is True

    svc = _Service(Cost(tier="http"))
    app.dependency_overrides[deps.require_api_key] = lambda: _key(10, suspended=True)
    app.dependency_overrides[deps.get_service] = lambda: svc
    r = client.post("/v1/scrape", json={"url": "https://example.com"})
    assert r.status_code == 401 and svc.calls == 0


def test_usage_requires_a_subject(client: TestClient) -> None:
    assert client.get("/internal/usage", headers=H).status_code == 400


def test_events_require_a_subject_and_pass_it_through(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_events(**kw: Any) -> list[dict[str, Any]]:
        seen.update(kw)
        return []

    monkeypatch.setattr(repo, "list_usage_events", fake_events)
    assert client.get("/internal/events", headers=H).status_code == 400
    r = client.get("/internal/events", headers=H, params={"owner_ref": "cust", "limit": 5})
    assert r.status_code == 200 and seen == {"owner_ref": "cust", "key_id": None, "limit": 5}


def test_put_credit_costs_validates_and_refreshes_the_process_table(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store: dict[str, int] = {"direct": 1}

    async def fake_set(table: dict[str, int]) -> None:
        store.update(table)

    async def fake_get() -> dict[str, int]:
        return dict(store)

    monkeypatch.setattr(repo, "set_credit_costs", fake_set)
    monkeypatch.setattr(repo, "get_credit_costs", fake_get)
    r = client.put("/internal/credit-costs", headers=H, json={"costs": {"browser": 5000}})
    assert r.status_code == 400
    r = client.put("/internal/credit-costs", headers=H, json={"costs": {"browser": 7}})
    assert r.status_code == 200 and r.json()["data"]["browser"] == 7


def test_scrape_refuses_an_exhausted_customer_before_fetching(client: TestClient) -> None:
    svc = _Service(Cost(tier="http"))
    app.dependency_overrides[deps.require_api_key] = lambda: _key(0)
    app.dependency_overrides[deps.get_service] = lambda: svc
    r = client.post("/v1/scrape", json={"url": "https://example.com"})
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "INSUFFICIENT_CREDITS"
    assert svc.calls == 0


def test_scrape_meters_a_successful_customer_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict[str, Any] = {}

    async def fake_record(key_id: str, **kw: Any) -> int:
        recorded.update(kw, key_id=key_id)
        return 1495

    async def fake_costs() -> dict[str, int]:
        return {"direct": 1, "proxied": 2, "browser": 5, "pdf_page": 1, "cached": 0}

    monkeypatch.setattr(repo, "record_usage", fake_record)
    monkeypatch.setattr(repo, "get_credit_costs", fake_costs)
    app.dependency_overrides[deps.require_api_key] = lambda: _key(1500)
    app.dependency_overrides[deps.get_service] = lambda: _Service(
        Cost(tier="browser", proxy_bytes=10)
    )
    r = client.post("/v1/scrape", json={"url": "https://example.com/page"})
    assert r.status_code == 200, r.text
    assert recorded["credits"] == 5
    assert recorded["owner_ref"] == "cust"
    assert recorded["endpoint"] == "scrape" and recorded["host"] == "example.com"


def test_operator_keys_are_metered_but_never_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metered like a customer, but never blocked on balance.

    The inverse of this test stood until 16 Sep 2026 — operator keys recorded
    nothing at all — which is how three keys ran on production for days and
    put not one row in usage_events. Zero credits here on purpose: the request
    must succeed AND be recorded.
    """
    recorded: dict[str, Any] = {}

    async def fake_record(key_id: str, **kw: Any) -> int:
        recorded.update({"key_id": key_id, **kw})
        return 0

    monkeypatch.setattr(repo, "record_usage", fake_record)
    app.dependency_overrides[deps.require_api_key] = lambda: _key(0, owner=OPERATOR_OWNER)
    app.dependency_overrides[deps.get_service] = lambda: _Service(Cost(tier="http"))
    assert client.post("/v1/scrape", json={"url": "https://example.com"}).status_code == 200
    assert recorded["owner_ref"] == OPERATOR_OWNER
    assert recorded["endpoint"] == "scrape"


def test_verify_key_names_the_owner_and_hides_unknown_keys(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get(plaintext: str) -> ApiKey | None:
        return _key(100, owner="cust-uuid") if plaintext == "sk_live_good_key_here" else None

    monkeypatch.setattr(repo, "get_api_key", fake_get)
    r = client.post("/internal/keys/verify", headers=H, json={"key": "sk_live_good_key_here"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["owner_ref"] == "cust-uuid"
    assert data["prefix"] == "sk_live_goo"
    assert "key" not in data  # the plaintext is never echoed back
    unknown = client.post("/internal/keys/verify", headers=H, json={"key": "sk_nope_nope"})
    assert unknown.status_code == 404
    # Never without the internal token: this is the one endpoint that maps a key to a person.
    bare = client.post("/internal/keys/verify", json={"key": "sk_live_good_key_here"})
    assert bare.status_code == 401


@pytest.mark.asyncio
async def test_charge_writes_the_credits_onto_the_cost_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_record(*a: Any, **kw: Any) -> int:
        return 99

    async def fake_table() -> dict[str, int]:
        return {}

    monkeypatch.setattr(repo, "record_usage", fake_record)
    monkeypatch.setattr(billing, "cost_table", fake_table)
    cost = Cost(tier="http")
    assert cost.credits is None
    await billing.charge(_key(100), endpoint="scrape", url="https://a.test", cost=cost)
    assert cost.credits == 1  # a plain page is one credit, and the response now says so


# -- rate-limited keys ------------------------------------------------------


class _FakeRedis:
    """Just enough of redis.asyncio for RateLimiter: counters and one hash per day."""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.hashes: dict[str, dict[str, int]] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + amount
        return bucket[field]

    async def hgetall(self, key: str) -> dict[str, str]:
        return {k: str(v) for k, v in self.hashes.get(key, {}).items()}


@pytest.mark.asyncio
async def test_rate_limiter_counts_only_refusals_per_key_per_day() -> None:
    import time as _time

    from engine.core.politeness import RateLimiter, refused_key

    fake = _FakeRedis()
    limiter = RateLimiter(fake)  # type: ignore[arg-type]
    results = [await limiter.check("key_a", 2) for _ in range(5)]
    assert [r[0] for r in results] == [True, True, False, False, False]
    await limiter.check("key_b", 100)

    day = _time.strftime("%Y-%m-%d", _time.gmtime())
    assert fake.hashes[refused_key(day)] == {"key_a": 3}
    assert await limiter.refused_on(day) == [{"key_id": "key_a", "refused": 3}]


@pytest.mark.asyncio
async def test_a_counting_failure_never_changes_the_429() -> None:
    from engine.core.politeness import RateLimiter

    class _Broken(_FakeRedis):
        async def hincrby(self, key: str, field: str, amount: int) -> int:
            raise RuntimeError("redis went away")

    limiter = RateLimiter(_Broken())  # type: ignore[arg-type]
    await limiter.check("key_a", 1)
    allowed, remaining, _ = await limiter.check("key_a", 1)
    assert allowed is False and remaining == 0


def test_rate_limited_endpoint_is_gated_and_reads_the_day(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engine.core import politeness

    seen: list[str] = []

    async def fake_refused_on(self: object, day: str) -> list[dict[str, Any]]:
        seen.append(day)
        return [{"key_id": "key_a", "refused": 40}]

    monkeypatch.setattr(politeness.RateLimiter, "refused_on", fake_refused_on)
    assert client.get("/internal/rate-limited").status_code == 401
    r = client.get("/internal/rate-limited", params={"day": "2026-09-25"}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"success": True, "data": [{"key_id": "key_a", "refused": 40}]}
    assert seen == ["2026-09-25"]
    assert client.get("/internal/rate-limited", params={"day": "nope"}, headers=H).status_code in (
        400,
        422,
    )
