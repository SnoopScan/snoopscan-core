"""The rest of the API contract: extract, search, crawl errors.

Interface compatibility with Firecrawl is deliberate — anything already
written against their API should migrate by changing a base URL — so these
assert the endpoints exist and behave, not just that they return 200.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.api import deps
from engine.api.app import app
from engine.core.fetch.base import FetchRequest, FetchResult
from engine.core.models import Tier
from engine.core.scrape_service import ScrapeService
from engine.settings import settings
from engine.storage import repositories as repo
from engine.storage.repositories import ApiKey

PRODUCT_PAGE = """
<html lang="en"><head><title>Mechanical Keyboard</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Mechanical Keyboard",
 "offers":{"@type":"Offer","price":"129.99","priceCurrency":"GBP",
 "availability":"https://schema.org/InStock"}}
</script></head><body><main><h1>Mechanical Keyboard</h1>
<p>A compact mechanical keyboard with hot-swappable switches, described here
in enough words that the extraction pipeline treats it as real content.</p>
</main></body></html>"""

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "price": {"type": "number"},
        "currency": {"type": "string"},
    },
    "required": ["name", "price"],
}


class StubFetcher:
    name = "http"

    async def fetch(self, req: FetchRequest) -> FetchResult:
        body = PRODUCT_PAGE.encode()
        return FetchResult(
            url=req.url,
            status_code=200,
            headers={},
            body=body,
            content_type="text/html; charset=utf-8",
            tier=self.name,
            latency_ms=10,
            bytes_transferred=len(body),
        )

    async def healthcheck(self) -> bool:
        return True


def _install() -> None:
    service = ScrapeService({Tier.HTTP: StubFetcher()}, persist=False)

    async def fake_key() -> ApiKey:
        return ApiKey(
            id="key_test",
            label="test",
            scopes=["scrape", "crawl", "map"],
            rate_limit_rpm=1000,
            allow_js_exec=False,
            webhook_secret=None,
            active=True,
            # Owned and in funds — see test_sdk.py.
            owner_ref="parity-test-owner",
            credits_remaining=10_000,
        )

    app.dependency_overrides[deps.require_api_key] = fake_key
    app.dependency_overrides[deps.get_service] = lambda: service


@pytest.fixture(autouse=True)
def _no_metering(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parity of the contract shapes is under test, not the ledger.

    The fake key exists only as a dependency override, so a real record_usage
    insert violates usage_events' foreign key.
    """

    async def fake_record(key_id: str, **kw: Any) -> int:
        return 0

    monkeypatch.setattr(repo, "record_usage", fake_record)

    # Charging reads the credit table from Postgres. Nothing here is about the
    # price, and the published suite runs with no database: without this the
    # extract tests passed only on a machine that had one.
    async def no_table() -> dict[str, int]:
        return {}

    monkeypatch.setattr(repo, "get_credit_costs", no_table)


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    _install()
    # The app's shutdown closes the pool and the Redis client. If an earlier
    # test left them bound to its own, now-closed loop, that close raises
    # "Event loop is closed" at teardown — which test ran first decided it,
    # so the published suite, missing some modules, failed where this one
    # passed. Start the app with neither, so it closes only what it opened.
    from engine.core import politeness
    from engine.storage import db

    politeness._redis = None
    db._pool = None
    with TestClient(app) as c:
        yield c


def post(client: TestClient, path: str, body: dict[str, Any]) -> Any:
    return client.post(path, json=body, headers={"Authorization": "Bearer k"})


# --------------------------------------------------------------------------
# /v1/extract
# --------------------------------------------------------------------------


def test_extract_endpoint_exists_and_returns_the_contract_shape(client: TestClient) -> None:
    response = post(
        client,
        "/v1/extract",
        {"urls": ["https://example.com/p/1"], "schema": SCHEMA},
    )
    assert response.status_code == 200
    rows = response.json()["data"]
    assert isinstance(rows, list)
    assert set(rows[0]) == {"url", "data", "confidence", "source", "error", "validation_errors"}


def test_extract_answers_from_structured_markup_without_a_model(
    client: TestClient,
) -> None:
    """Markup is free. A model call is only justified when it does not answer."""
    rows = post(
        client,
        "/v1/extract",
        {"urls": ["https://example.com/p/1"], "schema": SCHEMA},
    ).json()["data"]
    assert rows[0]["error"] is None
    assert rows[0]["data"]["name"] == "Mechanical Keyboard"
    assert rows[0]["data"]["price"] == 129.99


def test_extract_reports_a_missing_field_rather_than_inventing_one(
    client: TestClient,
) -> None:
    """The guarantee that makes the output trustworthy: absence is an error,
    never a plausible-looking value."""
    schema = {
        "type": "object",
        "properties": {"warrantyMonths": {"type": "number"}},
        "required": ["warrantyMonths"],
    }
    rows = post(
        client, "/v1/extract", {"urls": ["https://example.com/p/1"], "schema": schema}
    ).json()["data"]
    assert rows[0]["error"] is not None
    assert rows[0]["data"] is None or "warrantyMonths" not in (rows[0]["data"] or {})


def test_extract_per_url_failure_does_not_fail_the_request(client: TestClient) -> None:
    response = post(
        client,
        "/v1/extract",
        {"urls": ["https://example.com/p/1", "http://127.0.0.1/internal"], "schema": SCHEMA},
    )
    assert response.status_code == 200
    rows = response.json()["data"]
    assert len(rows) == 2
    assert rows[0]["error"] is None
    assert rows[1]["error"] is not None


def test_extract_rejects_too_many_urls(client: TestClient) -> None:
    response = post(
        client,
        "/v1/extract",
        {"urls": [f"https://example.com/{i}" for i in range(200)], "schema": SCHEMA},
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# /v1/search
# --------------------------------------------------------------------------


def test_search_switched_off_is_unavailable_not_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent told 'no results' concludes the thing does not exist. That is a
    different and worse answer than 'search is unavailable'.

    Search now needs no vendor — DuckDuckGo is the default — so this is the
    deployment that has turned search off on purpose.
    """
    monkeypatch.setattr(settings, "search_provider", "none")
    response = post(client, "/v1/search", {"query": "anything"})
    assert response.status_code >= 500
    body = response.json()
    assert body["success"] is False
    assert "provider" in body["error"]["message"].lower()


def test_search_validates_its_request(client: TestClient) -> None:
    assert post(client, "/v1/search", {"query": "x", "limit": 500}).status_code == 400
    assert post(client, "/v1/search", {}).status_code == 400


# --------------------------------------------------------------------------
# Contract surface
# --------------------------------------------------------------------------


def test_every_contract_endpoint_is_routed(client: TestClient) -> None:
    """The spec's endpoint list is the contract; a missing one is a gap for
    anything migrating from another provider."""
    schema = client.get("/openapi.json").json()["paths"]
    for path in (
        "/v1/scrape",
        "/v1/crawl",
        "/v1/crawl/{job_id}",
        "/v1/crawl/{job_id}/pages",
        "/v1/crawl/{job_id}/errors",
        "/v1/map",
        "/v1/batch/scrape",
        "/v1/extract",
        "/v1/search",
    ):
        assert path in schema, f"{path} is not routed"


def test_option_names_match_the_migration_contract(client: TestClient) -> None:
    """Option naming is deliberately shared, so a caller's existing request
    body works unchanged. A rename here silently breaks that."""
    from engine.core.models import CrawlRequest, ScrapeOptions

    scrape = set(ScrapeOptions.model_fields)
    for name in (
        "formats",
        "onlyMainContent",
        "includeTags",
        "excludeTags",
        "maxAge",
        "waitFor",
        "timeout",
        "actions",
        "headers",
        "mobile",
        "location",
        "proxy",
        "blockAssets",
        "removeBase64Images",
        "parsers",
    ):
        assert name in scrape, f"scrape option {name} was renamed"

    crawl = set(CrawlRequest.model_fields)
    for name in (
        "limit",
        "maxDepth",
        "includePaths",
        "excludePaths",
        "allowExternalLinks",
        "allowBackwardLinks",
        "ignoreSitemap",
        "ignoreQueryParameters",
        "deduplicateSimilarURLs",
        "delay",
        "scrapeOptions",
        "webhook",
    ):
        assert name in crawl, f"crawl option {name} was renamed"
