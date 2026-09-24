"""Every public endpoint is metered.

extract charges the fetch plus the model when it ran; search charges one flat
call plus every result page fetched; map charges one flat call. These routes
looked finished before — the metering seam existed and nothing called it.
Tests drive the real routes with a key that HAS an owner (operator keys are
never metered) and record what billing was asked to charge.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.api import billing, deps
from engine.api.app import app
from engine.api.routes import extract as extract_routes
from engine.core.credits import credits_for
from engine.core.models import Cost, Tier
from engine.core.scrape_service import ScrapeService
from engine.storage.repositories import ApiKey
from engine.tests.test_api_parity import StubFetcher

# A field no structured markup on the stub page can answer, so the model is needed.
PAGE_SCHEMA = {
    "type": "object",
    "properties": {"warranty_years": {"type": "integer"}},
    "required": ["warranty_years"],
}


@pytest.fixture
def charges(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def fake_charge(
        key: ApiKey, *, endpoint: str, url: str | None, cost: Cost, job_id: str | None = None
    ) -> int:
        seen.append({"endpoint": endpoint, "url": url, "cost": cost, "credits": credits_for(cost)})
        return 1

    monkeypatch.setattr(billing, "charge", fake_charge)
    return seen


def _client(credits: int) -> TestClient:
    service = ScrapeService({Tier.HTTP: StubFetcher()}, persist=False)

    async def fake_key() -> ApiKey:
        return ApiKey(
            id="key_metered",
            label="metered",
            scopes=["scrape", "crawl", "map", "search", "extract"],
            rate_limit_rpm=1000,
            allow_js_exec=False,
            webhook_secret=None,
            active=True,
            owner_ref="owner_1",
            credits_remaining=credits,
        )

    app.dependency_overrides[deps.require_api_key] = fake_key
    app.dependency_overrides[deps.get_service] = lambda: service
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


def _post(client: TestClient, path: str, body: dict[str, Any]) -> Any:
    return client.post(path, json=body, headers={"Authorization": "Bearer k"})


def test_extract_charges_the_fetch_and_the_model_when_it_ran(
    charges: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        extract_routes, "model_caller", lambda: lambda md, schema, prompt: {"warranty_years": 2}
    )
    with _client(50) as client:
        r = _post(
            client, "/v1/extract", {"urls": ["https://example.com/p/1"], "schema": PAGE_SCHEMA}
        )
    assert r.status_code == 200, r.text
    row = r.json()["data"][0]
    assert row["source"] == "model" and row["data"]["warranty_years"] == 2 and row["error"] is None
    assert len(charges) == 1
    charge = charges[0]
    assert charge["endpoint"] == "extract" and charge["url"] == "https://example.com/p/1"
    assert charge["cost"].tier == "http" and charge["cost"].extras == {"model_extract": 1}
    assert charge["credits"] == 1 + 5


def test_extract_without_a_model_charges_only_the_fetch_and_says_so(
    charges: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract_routes, "model_caller", lambda: None)
    with _client(50) as client:
        r = _post(
            client, "/v1/extract", {"urls": ["https://example.com/p/1"], "schema": PAGE_SCHEMA}
        )
    row = r.json()["data"][0]
    assert row["source"] is None and "No model extractor is configured" in row["error"]
    assert len(charges) == 1 and charges[0]["cost"].extras == {} and charges[0]["credits"] == 1


def test_search_charges_one_call_plus_every_page_it_fetched(
    charges: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Item:
        def __init__(self, url: str) -> None:
            self.url = url

        def to_payload(self) -> dict[str, Any]:
            return {"url": self.url, "title": "t", "description": "d"}

    class Provider:
        name = "fake"
        supports = frozenset({"country", "language", "page"})

        async def search(self, q: object) -> list[Item]:
            return [Item("https://example.com/a"), Item("https://example.com/b")]

    # Patch the rung, not the walk: the route now goes through the ladder, and
    # the billing this test guards must survive that.
    from engine.core import search as serp

    monkeypatch.setattr(serp, "ladder", lambda: [Provider()])
    with _client(50) as client:
        r = _post(
            client,
            "/v1/search",
            {"query": "kettles", "limit": 2, "scrapeOptions": {"formats": ["markdown"]}},
        )
    assert r.status_code == 200, r.text
    assert [c["endpoint"] for c in charges] == ["search", "search", "search"]
    assert charges[0]["cost"].extras == {"search": 1} and charges[0]["credits"] == 2
    assert charges[1]["cost"].tier == "http" and charges[1]["credits"] == 1


def test_map_charges_one_flat_call(charges: list[dict[str, Any]]) -> None:
    with _client(50) as client:
        r = _post(client, "/v1/map", {"url": "https://example.com", "limit": 10})
    assert r.status_code == 200, r.text
    assert len(charges) == 1
    assert (
        charges[0]["endpoint"] == "map"
        and charges[0]["cost"].extras == {"map": 1}
        and charges[0]["credits"] == 1
    )


def test_an_empty_balance_is_refused_before_anything_is_fetched(
    charges: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract_routes, "model_caller", lambda: None)
    with _client(0) as client:
        for path, body in [
            ("/v1/extract", {"urls": ["https://example.com/p/1"], "schema": PAGE_SCHEMA}),
            ("/v1/search", {"query": "kettles"}),
            ("/v1/map", {"url": "https://example.com"}),
        ]:
            r = _post(client, path, body)
            assert r.status_code == 402, (path, r.text)
            assert r.json()["error"]["code"] == "INSUFFICIENT_CREDITS"
    assert charges == []


def test_flat_extras_price_from_the_same_table() -> None:
    assert credits_for(Cost(extras={"search": 1})) == 2
    assert credits_for(Cost(extras={"map": 1})) == 1
    assert credits_for(Cost(tier="http", extras={"model_extract": 1})) == 6
    assert credits_for(Cost.from_cache(cache_own=True)) == 0
    assert credits_for(Cost.from_cache()) == 1, "another account's row is not free"
    # The operator's table LAYERS over the defaults; it does not replace them.
    # It used to: a key the desk had never saved priced at zero, which meant any
    # billable thing added after the operator last opened that screen shipped
    # free. Measured — a bought Google search billed 0 while the free rung
    # billed 2. Absence means "not configured"; only an explicit 0 means free.
    assert credits_for(Cost(extras={"search": 1}), {"direct": 1}) == 2
    assert credits_for(Cost(extras={"search": 1}), {"search": 7}) == 7
    assert credits_for(Cost(extras={"search": 1}), {"search": 0}) == 0
    # Never a fetch base for a non-fetch event.
    assert credits_for(Cost(extras={}), {"direct": 9}) == 0


def test_a_customer_key_is_handed_to_the_model_and_the_surcharge_is_waived(
    charges: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    specs: list[Any] = []

    def fake_caller(spec: Any = None) -> Any:
        specs.append(spec)
        return lambda md, schema, prompt: {"warranty_years": 2}

    monkeypatch.setattr(extract_routes, "model_caller", fake_caller)
    with _client(50) as client:
        r = _post(
            client,
            "/v1/extract",
            {
                "urls": ["https://example.com/p/1"],
                "schema": PAGE_SCHEMA,
                "model": {"provider": "openai", "apiKey": "sk-customer-key", "name": "gpt-4o-mini"},
            },
        )
    assert r.status_code == 200, r.text
    assert r.json()["data"][0]["source"] == "model"
    assert (
        specs[0].provider == "openai"
        and specs[0].api_key == "sk-customer-key"
        and specs[0].name == "gpt-4o-mini"
    )
    assert charges[0]["cost"].extras == {} and charges[0]["credits"] == 1
    assert "sk-customer-key" not in r.text


def test_a_bad_model_spec_is_a_400_not_a_500(charges: list[dict[str, Any]]) -> None:
    with _client(50) as client:
        r = _post(
            client,
            "/v1/extract",
            {
                "urls": ["https://example.com/p/1"],
                "schema": PAGE_SCHEMA,
                "model": {"provider": "other", "apiKey": "sk-customer-key"},
            },
        )
    assert r.status_code == 400 and "sk-customer-key" not in r.text
    assert charges == []
