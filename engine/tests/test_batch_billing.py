"""A batch page is a scraped page: it must be costed, metered and charged.

`_scrape_batch_url` used to discard the outcome of `service.scrape()`. Nothing
downstream noticed, because everything it should have driven is write-only:
`jobs.cost` stayed NULL, no `usage_events` row was written, and no credits came
off the owner. Proved against the running stack on 6 Sep 2026 with a real
customer key — two batch jobs delivered four pages and produced zero ledger
rows, while `GET /v1/batch/{id}` reported `creditsUsed` to that same customer.
The crawl path had done all three since it was written.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.core.models import Cost
from engine.workers.http_worker import HttpWorker


class _Metadata:
    wordCount = 120


class _Data:
    def __init__(self, cost: Cost) -> None:
        self.cost = cost
        self.metadata = _Metadata()


class _Outcome:
    def __init__(self, cost: Cost) -> None:
        self.data = _Data(cost)
        self.from_cache = False


class _Request:
    scrapeOptions = None


class _Service:
    def __init__(self, cost: Cost) -> None:
        self._cost = cost

    async def scrape(self, url: str, *a: Any, **kw: Any) -> _Outcome:
        return _Outcome(self._cost)


class _Repo:
    def __init__(self) -> None:
        self.costs: list[tuple[str, int, int, str, int]] = []
        self.completed: list[tuple[str, bool]] = []
        self.key: Any = object()

    async def complete_frontier_url(self, row_id: str, ok: bool) -> None:
        self.completed.append((row_id, ok))

    async def accumulate_job_cost(
        self, job_id: str, proxy_bytes: int, browser_ms: int, tier: str, credits: int = 0
    ) -> None:
        self.costs.append((job_id, proxy_bytes, browser_ms, tier, credits))

    async def store_page(self, row: dict[str, Any]) -> None:
        return None

    async def job_api_key(self, job_id: str) -> Any:
        return self.key


def _worker(cost: Cost) -> HttpWorker:
    w = HttpWorker.__new__(HttpWorker)  # no Redis/DB in construction
    w._service = _Service(cost)  # type: ignore[attr-defined]
    w.name = "test:1"  # type: ignore[attr-defined]
    return w


@pytest.fixture
def charged(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from engine.api import billing

    seen: list[dict[str, Any]] = []

    async def fake_charge(key: Any, **kw: Any) -> int:
        seen.append(kw)
        kw["cost"].credits = 7  # the real charge() writes this back onto the Cost
        return 0

    monkeypatch.setattr(billing, "charge", fake_charge)
    return seen


async def test_a_batched_page_accumulates_its_cost_on_the_job(
    monkeypatch: pytest.MonkeyPatch, charged: list[dict[str, Any]]
) -> None:
    """Without this `jobs.cost` is NULL for every batch, so the tier breakdown
    and the proxy/browser spend of a 10,000-URL batch are simply not recorded."""
    from engine.workers import http_worker as mod

    repo = _Repo()
    monkeypatch.setattr(mod, "repo", repo)
    cost = Cost(tier="impersonate", proxy_bytes=2048, browser_ms=350)

    await _worker(cost)._scrape_batch_url(
        "batch_1", {"id": "f1", "url": "https://x.test/"}, _Request()
    )

    assert repo.costs == [("batch_1", 2048, 350, "impersonate", 7)]


async def test_a_batched_page_is_charged_to_the_jobs_key(
    monkeypatch: pytest.MonkeyPatch, charged: list[dict[str, Any]]
) -> None:
    """The money half. `creditsUsed` is reported to the caller either way, so
    an uncharged batch is not a silent freebie — it is a printed claim the
    ledger does not support."""
    from engine.workers import http_worker as mod

    repo = _Repo()
    monkeypatch.setattr(mod, "repo", repo)
    cost = Cost(tier="http", proxy_bytes=10, browser_ms=0)

    await _worker(cost)._scrape_batch_url(
        "batch_1", {"id": "f1", "url": "https://x.test/p"}, _Request()
    )

    assert len(charged) == 1
    assert charged[0]["endpoint"] == "batch"
    assert charged[0]["url"] == "https://x.test/p"
    assert charged[0]["job_id"] == "batch_1"
    assert charged[0]["cost"] is cost


async def test_an_operator_key_batch_is_still_costed_but_not_charged(
    monkeypatch: pytest.MonkeyPatch, charged: list[dict[str, Any]]
) -> None:
    """`job_api_key` returns None for a job with no key. Cost accounting is
    operational and must still happen; only the charge is skipped."""
    from engine.workers import http_worker as mod

    repo = _Repo()
    repo.key = None
    monkeypatch.setattr(mod, "repo", repo)

    await _worker(Cost(tier="http"))._scrape_batch_url(
        "batch_1", {"id": "f1", "url": "https://x.test/"}, _Request()
    )

    assert repo.costs, "cost accounting is not the same thing as billing"
    assert charged == []


async def test_a_failed_batch_page_is_neither_costed_nor_charged(
    monkeypatch: pytest.MonkeyPatch, charged: list[dict[str, Any]]
) -> None:
    """A failed request is never charged (billing.py's own rule), and there is
    no outcome to cost."""
    from engine.core.errors import FetchFailed
    from engine.workers import http_worker as mod

    repo = _Repo()
    monkeypatch.setattr(mod, "repo", repo)

    worker = _worker(Cost(tier="http"))

    async def boom(*a: Any, **kw: Any) -> Any:
        raise FetchFailed("dead", {"stage": "dns"})

    worker._service.scrape = boom  # type: ignore[attr-defined]
    await worker._scrape_batch_url("batch_1", {"id": "f1", "url": "https://x.test/"}, _Request())

    assert repo.costs == []
    assert charged == []
    assert repo.completed == [("f1", False)]


async def test_the_credits_charged_are_banked_on_the_job(
    monkeypatch: pytest.MonkeyPatch, charged: list[dict[str, Any]]
) -> None:
    """`creditsUsed` on the status payload reads this counter, so the charge has
    to land here or the customer is told a number the ledger never recorded.

    Order matters and is the whole point: `billing.charge` is what fills
    `cost.credits`, so metering must happen BEFORE accumulation. Reversed, this
    banks a zero and every job reports `creditsUsed: 0`.
    """
    from engine.workers import http_worker as mod

    repo = _Repo()
    monkeypatch.setattr(mod, "repo", repo)

    await _worker(Cost(tier="http"))._scrape_batch_url(
        "batch_1", {"id": "f1", "url": "https://x.test/"}, _Request()
    )

    assert repo.costs[0][4] == 7, "metered after accumulating, so credits banked as 0"
