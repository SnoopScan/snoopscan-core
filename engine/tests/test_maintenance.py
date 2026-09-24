"""Scheduled maintenance: proxy health, usage rollup, directory health.

The directory tests carry the point of the whole file. A directory scraper
whose target changed its markup does not fail — it succeeds and returns twelve
items where it used to return four hundred. Every success metric stays green,
the ingest reports OK, and the data quietly stops arriving. The only way to
see it is to compare against the previous run, which is why
`previous_item_count` exists.
"""

from __future__ import annotations

import contextlib

import pytest

from engine.workers import scheduler
from engine.workers.scheduler import (
    DIRECTORY_COLLAPSE_RATIO,
    DIRECTORY_MIN_ITEMS,
    TASKS,
)

# --------------------------------------------------------------------------
# Task registration — a task that exists but is never scheduled does nothing
# --------------------------------------------------------------------------


def test_the_spec_s_maintenance_tasks_are_all_scheduled() -> None:
    """`should_retire` and `retire` existed for a while with nothing calling
    them, so a proxy that had stopped working stayed in the rotation being
    chosen and failing. Existing is not the same as running."""
    scheduled = {task.name for task in TASKS}
    required = {
        "frontier_reaper",
        "circuit_reset",
        "retention_sweep",
        "proxy_health",
        "directory_health",
        "proxy_usage_rollup",
        "domain_decay",
    }
    assert required <= scheduled, f"not scheduled: {required - scheduled}"


def test_every_task_has_a_sane_interval() -> None:
    for task in TASKS:
        assert task.interval_s >= 60, f"{task.name} runs more than once a minute"
        assert task.interval_s <= 604_800, f"{task.name} runs less than weekly"


def test_task_names_are_unique() -> None:
    names = [task.name for task in TASKS]
    assert len(names) == len(set(names))


# --------------------------------------------------------------------------
# Directory health — the silent failure
# --------------------------------------------------------------------------


class FakeDB:
    """Records the UPDATEs rather than running them."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.updates: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, _query: str, *_args: object) -> list[dict[str, object]]:
        return self._rows

    async def execute(self, query: str, *args: object) -> str:
        self.updates.append((query, args))
        return "UPDATE 1"


def directory(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "dir_1",
        "name": "Example Directory",
        "last_item_count": 400,
        "previous_item_count": 400,
        "last_ingested_at": "2026-09-01",
    }
    row.update(overrides)
    return row


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def install(rows: list[dict[str, object]]) -> FakeDB:
        db = FakeDB(rows)
        monkeypatch.setattr(scheduler, "db", db)
        return db

    return install


async def test_a_collapsed_directory_is_flagged(fake_db) -> None:  # type: ignore[no-untyped-def]
    """Twelve items where there were four hundred. Nothing errored."""
    db = fake_db([directory(last_item_count=12, previous_item_count=400)])
    flagged = await scheduler.check_directory_health()
    assert flagged == 1
    assert any("down from 400" in str(args) for _, args in db.updates)


async def test_a_healthy_directory_is_not_flagged(fake_db) -> None:  # type: ignore[no-untyped-def]
    db = fake_db([directory(last_item_count=395, previous_item_count=400)])
    assert await scheduler.check_directory_health() == 0
    assert any("health_note = NULL" in query for query, _ in db.updates)


async def test_normal_variation_does_not_cry_wolf(fake_db) -> None:  # type: ignore[no-untyped-def]
    """Directories genuinely shrink week to week. A check nobody believes is
    not a check, so the threshold sits well below normal variation."""
    just_above = int(400 * DIRECTORY_COLLAPSE_RATIO) + 1
    fake_db([directory(last_item_count=just_above, previous_item_count=400)])
    assert await scheduler.check_directory_health() == 0


async def test_a_small_directory_is_not_judged_proportionally(fake_db) -> None:  # type: ignore[no-untyped-def]
    """Three items becoming one is a 67% fall and means nothing."""
    fake_db([directory(last_item_count=1, previous_item_count=DIRECTORY_MIN_ITEMS - 1)])
    assert await scheduler.check_directory_health() == 0


async def test_a_directory_that_never_ingested_is_flagged(fake_db) -> None:  # type: ignore[no-untyped-def]
    db = fake_db([directory(last_ingested_at=None)])
    assert await scheduler.check_directory_health() == 1
    assert any("never ingested" in str(args) for _, args in db.updates)


async def test_a_recovered_directory_is_cleared(fake_db) -> None:  # type: ignore[no-untyped-def]
    """A flag that never clears becomes background noise and gets ignored."""
    db = fake_db([directory(last_item_count=400, previous_item_count=12)])
    assert await scheduler.check_directory_health() == 0
    assert any("health_note = NULL" in query for query, _ in db.updates)


# --------------------------------------------------------------------------
# Proxy health degrades cleanly without the proprietary layer
# --------------------------------------------------------------------------


async def test_proxy_health_is_a_noop_without_the_proxy_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The open core has to run without the proprietary modules present. A
    deployment with no proxy layer does nothing here rather than crashing the
    scheduler on startup."""
    import builtins

    real_import = builtins.__import__

    def refuse_proxy(name: str, *args: object, **kwargs: object) -> object:
        if "proxy" in name:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", refuse_proxy)
    assert await scheduler.check_proxy_health() == 0


# --------------------------------------------------------------------------
# Shutdown — a scheduler that accepts SIGTERM and then sleeps for a week
# --------------------------------------------------------------------------


async def test_stop_wakes_a_task_parked_on_its_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM must end the process now, not at the next interval boundary.

    `stop()` only sets a flag, and the flag is read at the TOP of each task
    loop. With a bare `asyncio.sleep(interval)` in the loop, a task parked on
    its interval never re-read it, and `asyncio.gather` waits for every runner
    — so `domain_decay`, which runs weekly, held the whole process open for up
    to seven days after the signal. `pkill` looked like a no-op twice before
    this was traced (6 Sep 2026). The waits are now interruptible.
    """
    import asyncio

    ran = 0

    async def once() -> int:
        nonlocal ran
        ran += 1
        return ran

    sched = scheduler.Scheduler(
        # A weekly task, exactly like domain_decay: the interval must never be
        # what decides how long shutdown takes.
        tasks=[scheduler.Task("weekly", 604_800, once)]
    )
    monkeypatch.setattr(scheduler.db, "close_pool", _noop)

    runner = asyncio.create_task(sched.run())
    await asyncio.sleep(0.05)  # let the task run once and park on its interval
    assert ran == 1

    sched.stop()
    # NOT wait_for: it CANCELS on timeout, `run()` swallows CancelledError and
    # returns cleanly, so the timeout never surfaced and this test passed
    # against the bug it exists for. Wait without cancelling, then ask.
    await asyncio.wait([runner], timeout=2)
    stopped = runner.done()
    runner.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner
    assert stopped, "stop() left the scheduler parked on its 7-day interval"


async def test_stop_before_run_does_not_explode() -> None:
    """`_stopping` only exists once the loop is running; stopping a scheduler
    that never started must not raise."""
    scheduler.Scheduler(tasks=[]).stop()


async def _noop() -> None:
    return None
