"""A job whose handler dies must not sit `queued` forever (6 Sep 2026).

`JobQueue.pop` is destructive — BRPOP removes the message — so if `handle`
raises before the job reaches `running` or a terminal state, the message is
gone and nothing is left to run the job. Observed live: a crawl orphaned at
`queued` because a stale worker from an earlier session popped it and dropped
it. The worker now drives such a job to FAILED so a caller sees it.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.core.models import JobStatus
from engine.workers.http_worker import HttpWorker


class _FakeRepo:
    def __init__(self, status: str) -> None:
        self.status = status
        self.failed_with: dict[str, Any] | None = None

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        if self.status is None:
            return None
        return {"id": job_id, "status": self.status, "kind": "crawl", "input": {}}

    async def set_job_status(self, job_id: str, status: str, error: Any = None) -> None:
        self.status = str(status)
        self.failed_with = {"status": str(status), "error": error}


def _worker() -> HttpWorker:
    return HttpWorker.__new__(HttpWorker)  # no Redis/DB in construction


@pytest.mark.parametrize("start", ["queued", "running"])
async def test_a_non_terminal_orphan_is_marked_failed(monkeypatch: Any, start: str) -> None:
    from engine.workers import http_worker as mod

    repo = _FakeRepo(start)
    monkeypatch.setattr(mod, "repo", repo)
    await _worker()._fail_orphaned("crawl_1", RuntimeError("seed blew up"))
    assert repo.failed_with is not None
    assert repo.failed_with["status"] == str(JobStatus.FAILED)
    assert "RuntimeError" in repo.failed_with["error"]["message"]


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_a_job_that_reached_its_own_terminal_state_is_left_alone(
    monkeypatch: Any, terminal: str
) -> None:
    from engine.workers import http_worker as mod

    repo = _FakeRepo(terminal)
    monkeypatch.setattr(mod, "repo", repo)
    await _worker()._fail_orphaned("crawl_1", RuntimeError("late error"))
    assert repo.failed_with is None, f"{terminal} must not be overwritten"


async def test_a_vanished_job_is_not_resurrected(monkeypatch: Any) -> None:
    from engine.workers import http_worker as mod

    repo = _FakeRepo(None)  # get_job returns None
    monkeypatch.setattr(mod, "repo", repo)
    await _worker()._fail_orphaned("crawl_gone", RuntimeError("x"))
    assert repo.failed_with is None


async def test_failing_to_fail_is_swallowed(monkeypatch: Any) -> None:
    """A repo error while marking the orphan failed must not kill the worker."""
    from engine.workers import http_worker as mod

    class _Broken:
        async def get_job(self, job_id: str) -> dict[str, Any]:
            return {"id": job_id, "status": "queued", "kind": "crawl", "input": {}}

        async def set_job_status(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("db down")

    monkeypatch.setattr(mod, "repo", _Broken())
    await _worker()._fail_orphaned("crawl_1", RuntimeError("x"))  # must not raise
