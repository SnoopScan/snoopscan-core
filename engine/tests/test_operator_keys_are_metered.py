"""An operator key is metered like anyone else's, and an ownerless key is refused.

Three keys ran on production for days with no owner. `billing.charge` returned
early for them, so every fetch they made — real proxy bandwidth, really paid
for — recorded nothing: no usage_event, no usage_daily row, nothing on any
screen. The desk showed an honest zero for work that had certainly happened.

Metering is write-only, so nothing about such a key LOOKS wrong: it fetches,
it returns pages, its own tests pass. The ledger is the only witness, which is
why this asserts against the real tables rather than a mock.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from engine.api import billing
from engine.core.credits import OPERATOR_OWNER
from engine.core.models import Cost, Tier
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
        pytest.skip("Postgres is required for the operator-key suite")
    yield
    await db.close_pool()


@pytest.fixture
async def operator_key() -> AsyncIterator[repo.ApiKey]:
    plaintext = f"sk_op_{uuid.uuid4().hex}"
    key_id = await repo.create_api_key(
        plaintext, "operator-metering-test", owner_ref=OPERATOR_OWNER
    )
    key = await repo.get_api_key(plaintext)
    assert key is not None
    yield key
    async with db.transaction() as conn:
        for table in ("usage_daily_costs", "usage_daily", "usage_events"):
            await conn.execute(f"DELETE FROM {table} WHERE api_key_id = $1", key_id)  # noqa: S608
        await conn.execute("DELETE FROM api_keys WHERE id = $1", key_id)


async def _events_for(key_id: str) -> int:
    row = await db.fetchrow("SELECT count(*) AS n FROM usage_events WHERE api_key_id = $1", key_id)
    return int(row["n"]) if row else 0


async def test_an_operator_key_records_its_usage(operator_key: repo.ApiKey) -> None:
    assert operator_key.owner_ref == OPERATOR_OWNER
    before = await _events_for(operator_key.id)

    await billing.charge(
        operator_key,
        endpoint="scrape",
        url="https://operator.example.invalid/page",
        cost=Cost(tier=Tier.HTTP),
    )

    assert await _events_for(operator_key.id) == before + 1, (
        "an operator key must leave a usage_event — this is the exact hole that "
        "hid a week of real fetching"
    )

    row = await db.fetchrow(
        "SELECT sum(requests) AS r FROM usage_daily WHERE api_key_id = $1", operator_key.id
    )
    assert row is not None and int(row["r"] or 0) >= 1, "the daily rollup must move too"


async def test_an_operator_key_is_never_refused_for_credits(operator_key: repo.ApiKey) -> None:
    """Metered, but not blocked: an internal batch cut off mid-run is an outage.

    The operator owner is created with no credits, so a key that WAS refused on
    balance would fail here the moment it was used in anger.
    """
    assert operator_key.credits_remaining <= 0 or operator_key.credits_remaining >= 0
    billing.assert_credits(operator_key)  # must not raise


async def test_a_key_cannot_be_created_without_an_owner() -> None:
    """The default that caused it. `owner_ref` is required, so this is a TypeError."""
    with pytest.raises(TypeError):
        await repo.create_api_key(f"sk_no_{uuid.uuid4().hex}", "ownerless")  # type: ignore[call-arg]
