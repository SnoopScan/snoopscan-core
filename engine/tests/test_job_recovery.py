"""A crawl interrupted by a worker stop or crash must be picked back up.

Found 11 Sep 2026 by a colleague reading the worker against spec 07. A job is
carried by ONE Redis message and `pop` destroys it, so:

  * on SIGTERM `_run_crawl` logged `crawl_paused_for_shutdown` and returned,
    `_run_batch` skipped `_finish`, nothing re-queued the job, and the claimed
    rows waited out the five-minute reaper instead of being released (§2);
  * on a crash the reaper freed the URLs, and nothing ever re-drove the job
    (§1, §11 — "job retried").

Three jobs were stuck that way, the oldest since 1 Sep. The runbook restarts
the worker after every pull, and the stale-worker warning added on 9 Sep tells
operators to restart it — so every deploy that caught a crawl orphaned it.

Two paths now: a draining worker hands its job back at once, and the scheduler
re-drives anything a crash left behind.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from engine.workers import scheduler
from engine.workers.http_worker import HttpWorker

# --------------------------------------------------------------------------
# the draining worker
# --------------------------------------------------------------------------


class _Pushes:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def push(self, message: Any, queue: Any = None, priority: Any = None) -> None:
        self.messages.append(message)


def _draining_worker(pushes: _Pushes) -> HttpWorker:
    w = HttpWorker.__new__(HttpWorker)  # no Redis/DB in construction
    w.name = "Mac:1"
    w._draining = True
    w._current_job = None
    w._queue = pushes
    return w


@pytest.mark.parametrize("kind", ["crawl", "batch"])
async def test_a_draining_worker_hands_its_job_back(monkeypatch: Any, kind: str) -> None:
    """Batch is here on purpose: it did not even log, it just dropped the job."""
    from engine.workers import http_worker as mod

    released: list[tuple[str, str]] = []

    async def _release(job_id: str, worker: str) -> int:
        released.append((job_id, worker))
        return 1

    monkeypatch.setattr(mod.repo, "release_claims", _release)
    pushes = _Pushes()

    await _draining_worker(pushes)._hand_back({"id": f"{kind}_1", "kind": kind}, processed=7)

    assert released == [(f"{kind}_1", "Mac:1")], "its OWN claims go back, at once"
    assert len(pushes.messages) == 1, "and the job goes back on the queue"
    assert pushes.messages[0].job_id == f"{kind}_1"
    assert pushes.messages[0].kind == kind, "a batch must not come back as a crawl"


async def test_a_hand_back_that_cannot_reach_redis_does_not_crash_shutdown(
    monkeypatch: Any,
) -> None:
    """The scheduler's reaper is the backstop; shutdown must still complete."""
    from engine.workers import http_worker as mod

    async def _release(job_id: str, worker: str) -> int:
        return 0

    class _Down:
        async def push(self, *a: Any, **k: Any) -> None:
            raise ConnectionError("redis down")

    monkeypatch.setattr(mod.repo, "release_claims", _release)
    w = _draining_worker(_Pushes())
    w._queue = _Down()

    await w._hand_back({"id": "crawl_1", "kind": "crawl"}, processed=0)  # must not raise


async def test_a_resumed_job_is_not_seeded_again(monkeypatch: Any) -> None:
    """The seeding test was "no PENDING rows". A batch whose last URL finished
    before its worker died has none — and would re-insert, re-scrape and
    re-bill every URL. The question is "has it ever been seeded"."""
    from engine.workers import http_worker as mod

    seeded: list[Any] = []

    class _Repo:
        async def set_job_status(self, *a: Any, **k: Any) -> None: ...
        async def frontier_row_count(self, job_id: str) -> int:
            return 40  # seeded long ago; every row already done

        async def pending_frontier_count(self, job_id: str) -> int:
            return 0

        async def add_frontier_urls(self, job_id: str, rows: Any) -> None:
            seeded.append(rows)

        async def claim_frontier_url(self, job_id: str, worker: str) -> None:
            return None

        async def refresh_job_counters(self, job_id: str) -> None: ...

    monkeypatch.setattr(mod, "repo", _Repo())
    w = HttpWorker.__new__(HttpWorker)
    w.name, w._draining = "Mac:1", False

    async def _nothing(*a: Any, **k: Any) -> None: ...

    w._notify = _nothing  # type: ignore[method-assign]
    w._finish = _nothing  # type: ignore[method-assign]

    await w._run_batch(
        {
            "id": "batch_1",
            "kind": "batch",
            "input": {"urls": ["https://example.com/a"]},
            "webhook_url": None,
            "webhook_events": [],
        }
    )

    assert seeded == [], "a job with frontier rows was seeded a second time"


# --------------------------------------------------------------------------
# the scheduler's resumer
# --------------------------------------------------------------------------


def _job(**kw: Any) -> dict[str, Any]:
    base = {
        "id": "crawl_1",
        "kind": "crawl",
        "status": "running",
        "resumes": 0,
        "input": {"url": "https://example.com", "limit": 100},
        "claimed": 0,
        "pending": 40,
        "done": 10,
        "last_activity": datetime.now(UTC) - timedelta(minutes=10),
    }
    base.update(kw)
    return base


class _World:
    """Redis and Postgres as the resumer sees them."""

    def __init__(self, jobs: list[dict[str, Any]], queued: Any = (), held: Any = ()) -> None:
        self.jobs = jobs
        self.queued = None if queued is None else set(queued)
        self.held = None if held is None else set(held)
        self.pushed: list[str] = []
        self.resumed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def install(self, monkeypatch: Any) -> None:
        from engine.workers import queue as q

        world = self

        async def _queued() -> Any:
            return world.queued

        async def _held() -> Any:
            return world.held

        async def _candidates(idle: int) -> list[dict[str, Any]]:
            return world.jobs

        async def _mark(job_id: str) -> int:
            world.resumed.append(job_id)
            return 1

        async def _fail(job_id: str, message: str) -> None:
            world.failed.append((job_id, message))

        class _Q:
            async def push(self, message: Any, *a: Any, **k: Any) -> None:
                world.pushed.append(message.job_id)

        monkeypatch.setattr(q, "queued_job_ids", _queued)
        monkeypatch.setattr(q, "held_job_ids", _held)
        monkeypatch.setattr(q, "JobQueue", _Q)
        monkeypatch.setattr(scheduler.repo, "orphaned_job_candidates", _candidates)
        monkeypatch.setattr(scheduler.repo, "mark_job_resumed", _mark)
        monkeypatch.setattr(scheduler.repo, "fail_interrupted_job", _fail)


async def test_an_orphaned_job_is_resumed(monkeypatch: Any) -> None:
    world = _World([_job()])
    world.install(monkeypatch)

    out = await scheduler.resume_orphaned_jobs()

    assert world.pushed == ["crawl_1"] and world.resumed == ["crawl_1"]
    assert out["resumed"] == 1


async def test_a_job_whose_message_is_waiting_is_left_alone(monkeypatch: Any) -> None:
    """Queued behind other work, or waiting for a worker to start. Pushing it
    again would run it twice."""
    world = _World([_job()], queued={"crawl_1"})
    world.install(monkeypatch)

    await scheduler.resume_orphaned_jobs()

    assert world.pushed == [] and world.failed == []


async def test_a_live_worker_between_two_urls_is_not_second_guessed(monkeypatch: Any) -> None:
    """The race. Nothing claimed and no message is also what a HEALTHY crawl
    looks like for the instant between one URL and the next. Only the worker
    saying it holds the job tells them apart."""
    world = _World([_job()], held={"crawl_1"})
    world.install(monkeypatch)

    await scheduler.resume_orphaned_jobs()

    assert world.pushed == [], "two workers on one crawl"


async def test_a_job_idle_for_days_with_work_left_is_failed_not_billed(monkeypatch: Any) -> None:
    """Resuming bills new pages. A caller who stopped waiting a week ago should
    not be charged for a crawl finishing now."""
    world = _World([_job(last_activity=datetime.now(UTC) - timedelta(days=4))])
    world.install(monkeypatch)

    await scheduler.resume_orphaned_jobs()

    assert world.pushed == []
    assert [j for j, _ in world.failed] == ["crawl_1"]
    assert "still available" in world.failed[0][1], "say the pages are not lost"


async def test_a_job_that_keeps_being_orphaned_is_failed(monkeypatch: Any) -> None:
    world = _World([_job(resumes=scheduler.MAX_RESUMES)])
    world.install(monkeypatch)

    await scheduler.resume_orphaned_jobs()

    assert world.pushed == [] and [j for j, _ in world.failed] == ["crawl_1"]
    assert "Interrupted 5 times" in world.failed[0][1]


async def test_an_old_crawl_at_its_limit_is_finished_not_failed(monkeypatch: Any) -> None:
    """The 6 Sep job: 500 of 500 done, 68 pending, stuck `running`. Resuming
    bills nothing — process_one stops at the limit and `_finish` lands the 68
    in `skipped`. Failing it would call a complete crawl a failure."""
    world = _World(
        [
            _job(
                id="crawl_full",
                pending=68,
                done=500,
                input={"url": "https://x.test", "limit": 500},
                last_activity=datetime.now(UTC) - timedelta(days=5),
            )
        ]
    )
    world.install(monkeypatch)

    await scheduler.resume_orphaned_jobs()

    assert world.pushed == ["crawl_full"] and world.failed == []


async def test_it_does_nothing_at_all_when_it_cannot_see_redis(monkeypatch: Any) -> None:
    """ "Nothing queued" read off an outage would re-queue every active job."""
    world = _World([_job(), _job(id="crawl_2")], queued=None)
    world.install(monkeypatch)

    out = await scheduler.resume_orphaned_jobs()

    assert world.pushed == [] and world.failed == []
    assert out["blind"] == 1


def test_the_resume_bound_waits_out_the_claim_timeout() -> None:
    """The resumer must look only after `reap_frontier` has handed a dead
    worker's claims back — otherwise a resumed job finishes with URLs still
    claimed, and `skip_pending_frontier` writes them off as skipped."""
    from engine.workers.queue import CLAIM_TIMEOUT_S

    assert scheduler.RESUME_MARGIN_S > 0
    assert "job_resumer" in [t.name for t in scheduler.TASKS]
    assert CLAIM_TIMEOUT_S + scheduler.RESUME_MARGIN_S > CLAIM_TIMEOUT_S


# --------------------------------------------------------------------------
# the runners themselves must REACH the hand-back
# --------------------------------------------------------------------------
#
# The tests above call `_hand_back` directly, and a negative control proved
# that was not enough: with `_run_crawl` put back to "log and return" on drain,
# every one of them still passed. A function that works is no use if the path
# that should call it does not. These drive the runners.


def _runner_worker(pushes: _Pushes) -> HttpWorker:
    w = _draining_worker(pushes)

    async def _nothing(*a: Any, **k: Any) -> None: ...

    w._notify = _nothing  # type: ignore[method-assign]
    w._notify_page = _nothing  # type: ignore[method-assign]
    w._finish = _nothing  # type: ignore[method-assign]
    return w


class _RunnerRepo:
    def __init__(self) -> None:
        self.released: list[str] = []

    async def set_job_status(self, *a: Any, **k: Any) -> None: ...
    async def frontier_row_count(self, job_id: str) -> int:
        return 12  # already seeded

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return {"id": job_id, "status": "running"}

    async def claim_frontier_url(self, job_id: str, worker: str) -> None:
        return None

    async def refresh_job_counters(self, job_id: str) -> None: ...
    async def release_claims(self, job_id: str, worker: str) -> int:
        self.released.append(job_id)
        return 0


async def test_a_crawl_interrupted_by_shutdown_goes_back_on_the_queue(monkeypatch: Any) -> None:
    from engine.workers import http_worker as mod

    repo = _RunnerRepo()
    monkeypatch.setattr(mod, "repo", repo)
    pushes = _Pushes()

    await _runner_worker(pushes)._run_crawl(
        {
            "id": "crawl_9",
            "kind": "crawl",
            "input": {"url": "https://example.com", "limit": 10},
            "webhook_url": None,
            "webhook_events": [],
        }
    )

    assert [m.job_id for m in pushes.messages] == ["crawl_9"], "the crawl was dropped on drain"
    assert repo.released == ["crawl_9"]


async def test_a_batch_interrupted_by_shutdown_goes_back_on_the_queue(monkeypatch: Any) -> None:
    """Batch was the worse of the two: it did not even log the drop."""
    from engine.workers import http_worker as mod

    repo = _RunnerRepo()
    monkeypatch.setattr(mod, "repo", repo)
    pushes = _Pushes()

    await _runner_worker(pushes)._run_batch(
        {
            "id": "batch_9",
            "kind": "batch",
            "input": {"urls": ["https://example.com/a"]},
            "webhook_url": None,
            "webhook_events": [],
        }
    )

    assert [m.job_id for m in pushes.messages] == ["batch_9"], "the batch was dropped on drain"
