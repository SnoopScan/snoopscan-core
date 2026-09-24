"""Change history belongs to the account that asked for it.

`page_versions` was keyed by URL alone (found 23 Sep 2026 while preparing the
directory listings): a customer's first check of a URL could answer
"unchanged since <time>", and that time was when ANOTHER customer had fetched
it. Runs the real SQL; skipped without Postgres, like the other integration
suites.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

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
        pytest.skip("Postgres is required for the change-history suite")
    yield
    await db.close_pool()


@pytest.fixture
async def page() -> AsyncIterator[bytes]:
    norm = uuid.uuid4().bytes
    yield norm
    await db.execute("DELETE FROM page_versions WHERE normalized_hash = $1", norm)


async def test_one_accounts_history_is_invisible_to_another(page: bytes) -> None:
    url = "https://example.com/pricing"
    first = repo.content_hash("v1")

    assert (await repo.record_version(page, url, first, 2, "owner-a"))[0] == "new"
    assert (await repo.record_version(page, url, first, 2, "owner-a"))[0] == "same"

    status, previous_at = await repo.record_version(page, url, first, 2, "owner-b")
    assert status == "new", "B has never looked; A's capture is none of B's business"
    assert previous_at is None


async def test_each_account_sees_its_own_changes(page: bytes) -> None:
    url = "https://example.com/pricing"
    await repo.record_version(page, url, repo.content_hash("v1"), 2, "owner-a")
    await repo.record_version(page, url, repo.content_hash("v1"), 2, "owner-b")

    assert (await repo.record_version(page, url, repo.content_hash("v2"), 2, "owner-a"))[
        0
    ] == "changed"
    assert (await repo.record_version(page, url, repo.content_hash("v1"), 2, "owner-b"))[
        0
    ] == "same"


async def test_rows_from_before_owners_match_nobody(page: bytes) -> None:
    await db.execute(
        "INSERT INTO page_versions (normalized_hash, url, content_hash, word_count) "
        "VALUES ($1, $2, $3, 1)",
        page,
        "https://example.com/old",
        repo.content_hash("old"),
    )
    assert (
        await repo.record_version(
            page, "https://example.com/old", repo.content_hash("old"), 1, "owner-a"
        )
    )[0] == "new"
