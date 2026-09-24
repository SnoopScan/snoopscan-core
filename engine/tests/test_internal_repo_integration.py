"""The repository functions the operator app relies on, against the REAL database.

The unit tests for /internal monkeypatch the repository, which is right for
the gate and the contract but blind to SQL: an endpoint selecting a column
that does not exist passes every unit test and 500s in production. These run
the queries. Skipped when Postgres is not available, like the crawl suite.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from engine.core.credits import DEFAULT_COSTS
from engine.storage import db
from engine.storage import repositories as repo

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module", autouse=True)
async def _require_db() -> AsyncIterator[None]:
    try:
        ok = await db.healthy()
    except Exception:
        ok = False
    if not ok:
        pytest.skip("Postgres is required for the repository integration suite")
    yield
    # The pool is bound to this module's event loop. Leave it open and the next
    # integration module's health check raises "Event loop is closed", which its
    # skip-on-exception fixture reads as "no database" — 22 tests silently skip.
    await db.close_pool()


@pytest.fixture
async def owner() -> AsyncIterator[str]:
    ref = f"test-owner-{uuid.uuid4()}"
    yield ref
    # Tear down everything this test created, in dependency order.
    async with db.transaction() as conn:
        keys = "(SELECT id FROM api_keys WHERE owner_ref = $1)"
        for table in ("usage_daily", "usage_events"):
            await conn.execute(
                f"DELETE FROM {table} WHERE api_key_id IN {keys}",  # noqa: S608
                ref,
            )
        await conn.execute("DELETE FROM api_keys WHERE owner_ref = $1", ref)
        await conn.execute("DELETE FROM owners WHERE owner_ref = $1", ref)


async def test_key_lifecycle_and_owner_balance(owner: str) -> None:
    key_id = await repo.create_api_key("sk_test_" + uuid.uuid4().hex, "Prod", owner_ref=owner)
    keys = await repo.list_api_keys(owner)
    assert [k["id"] for k in keys] == [key_id]
    assert keys[0]["prefix"].startswith("sk_test_") and keys[0]["owner_ref"] == owner

    assert (await repo.get_owner(owner))["credits_remaining"] == 0
    assert await repo.adjust_credits(owner, 1500) == 1500
    assert await repo.adjust_credits(owner, -5000) == 0  # floors at zero

    assert await repo.update_api_key(key_id, label="Renamed", active=False)
    assert (await repo.list_api_keys(owner))[0]["active"] is False
    with pytest.raises(ValueError):
        await repo.update_api_key(key_id, owner_ref="someone-else")


async def test_metering_writes_event_rollup_and_balance(owner: str) -> None:
    key_id = await repo.create_api_key("sk_test_" + uuid.uuid4().hex, "Meter", owner_ref=owner)
    await repo.adjust_credits(owner, 100)
    left = await repo.record_usage(
        key_id,
        owner_ref=owner,
        endpoint="scrape",
        host="example.com",
        tier="browser",
        proxy_bytes=0,
        cached=False,
        pdf_pages=0,
        credits=5,
    )
    assert left == 95
    left = await repo.record_usage(
        key_id,
        owner_ref=owner,
        endpoint="scrape",
        host="example.com",
        tier="http",
        proxy_bytes=10,
        cached=False,
        pdf_pages=0,
        credits=2,
    )
    assert left == 93

    summary = await repo.usage_summary(owner_ref=owner, days=1)
    assert summary["requests"] == 2 and summary["credits"] == 7
    day = summary["daily"][0]
    assert day["browser"] == 1 and day["proxied"] == 1 and day["direct"] == 0

    by_key = await repo.usage_summary(key_id=key_id, days=1)
    assert by_key["credits"] == 7
    assert summary["by_endpoint"] == [{"endpoint": "scrape", "requests": 2, "credits": 7}]

    events = await repo.list_usage_events(owner_ref=owner, limit=10)
    assert [e["credits"] for e in events] == [2, 5]  # newest first
    assert events[0]["key_label"] == "Meter" and events[0]["host"] == "example.com"


async def test_suspended_and_concurrency_round_trip(owner: str) -> None:
    await repo.ensure_owner(owner)
    assert await repo.update_owner(owner, suspended=True, concurrency=42)
    o = await repo.get_owner(owner)
    assert o["suspended"] is True and o["concurrency"] == 42
    key_id = await repo.create_api_key("sk_test_" + uuid.uuid4().hex, "S", owner_ref=owner)
    assert key_id
    row = await db.fetchrow("SELECT key_hash FROM api_keys WHERE id = $1", key_id)
    assert row is not None


async def test_credit_costs_round_trip() -> None:
    before = await repo.get_credit_costs()
    try:
        await repo.set_credit_costs({"browser": 9})
        assert (await repo.get_credit_costs())["browser"] == 9
    finally:
        await repo.set_credit_costs({"browser": before.get("browser", DEFAULT_COSTS["browser"])})


async def test_proxy_provider_round_trip_with_encrypted_secret() -> None:
    from cryptography.fernet import Fernet

    from engine import settings as settings_mod

    settings_mod.get_settings().encryption_key = Fernet.generate_key().decode()
    row = await repo.create_proxy_provider(
        {
            "name": "Test Vendor",
            "type": "residential",
            "host": "geo.example.test",
            "port": 12321,
            "username": "user",
            "password": "s3cret",
            "country": "us",
            "priority": 5,
        }
    )
    try:
        assert row["id"].startswith("prov_") and "password" not in row and "password_enc" not in row
        enc = await db.fetchval("SELECT password_enc FROM proxy_providers WHERE id = $1", row["id"])
        assert enc != "s3cret" and "s3cret" not in enc
        assert await repo.get_proxy_provider_secret(row["id"]) == "s3cret"
        assert any(p["id"] == row["id"] for p in await repo.list_proxy_providers(enabled_only=True))
        assert await repo.update_proxy_provider(row["id"], {"enabled": False, "password": "new"})
        assert await repo.get_proxy_provider_secret(row["id"]) == "new"
        assert not any(
            p["id"] == row["id"] for p in await repo.list_proxy_providers(enabled_only=True)
        )
    finally:
        assert await repo.delete_proxy_provider(row["id"])


async def test_operator_reads_run_against_real_columns() -> None:
    h = await repo.health_summary()
    for key in (
        "database",
        "jobs_24h",
        "success_rate_24h",
        "p50_latency_ms",
        "tier_mix_24h",
        "queue_depth",
        "proxy_today",
        "domains",
        "circuits_open",
        "active_keys",
        "proxies",
    ):
        assert key in h, key
    assert set(h["queue_depth"]) == {"jobs_queued", "jobs_running", "frontier_pending"}
    assert set(h["proxy_today"]) == {"bytes", "requests", "budget_mb"}

    for sort in ("recent", "hardest", "blocked", "busiest"):
        rows = await repo.list_domain_profiles(5, sort)
        assert isinstance(rows, list)
    rows = await repo.list_domain_profiles(5, "recent", q="zzz-no-such-domain")
    assert rows == []

    assert isinstance(await repo.list_proxies(), list)
    assert isinstance(await repo.list_jobs(5), list)
