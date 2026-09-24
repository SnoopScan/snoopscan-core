"""Monitors: watch pages for changes on a schedule (6 Sep 2026).

The comparison is /v1/scrape's own change tracking; these tests pin what the
monitor adds — the schedule, the per-page statuses, the counts, the webhook
only when something is worth saying — with a stub service, and the request
shape through the real app.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from engine.core import monitor as mon
from engine.core.errors import TargetError
from engine.core.models import Cost, PageMetadata, ScrapeData


class _Row(dict):
    """asyncpg.Record's surface: `[]` and `in`."""


def _monitor(**over: Any) -> _Row:
    row = _Row(
        id="mon_1",
        api_key_id="key_1",
        name="pricing",
        urls=["https://a.test/p", "https://b.test/q"],
        interval_minutes=30,
        goal=None,
        webhook_url=None,
        active=True,
    )
    row.update(over)
    return row


class _Service:
    """Returns a changeTracking payload per URL, or raises."""

    def __init__(self, tracking: dict[str, Any]) -> None:
        self.tracking, self.calls = tracking, []

    async def scrape(self, url: str, options: Any, **kw: Any) -> Any:
        self.calls.append((url, options))
        t = self.tracking.get(url)
        if isinstance(t, Exception):
            raise t

        class _O:
            pass

        o = _O()
        o.data = ScrapeData(  # type: ignore[attr-defined]
            markdown="# x",
            metadata=PageMetadata(url=url, sourceURL=url),
            cost=Cost(tier="http"),
            changeTracking=t,
        )
        return o


@pytest.fixture
def quiet_repo(monkeypatch: Any) -> dict[str, Any]:
    """The repository calls the runner makes, captured instead of executed."""
    from engine.storage import repositories as repo

    seen: dict[str, Any] = {"checks": [], "runs": []}

    async def key_by_id(key_id: str) -> Any:
        return None  # no billing in the unit test

    async def insert(monitor_id: str, *, pages: Any, counts: Any, triggered_by: str) -> str:
        seen["checks"].append(
            {"monitor_id": monitor_id, "pages": pages, "counts": counts, "by": triggered_by}
        )
        return "chk_1"

    async def mark(monitor_id: str, interval: int) -> None:
        seen["runs"].append((monitor_id, interval))

    monkeypatch.setattr(repo, "api_key_by_id", key_by_id)
    monkeypatch.setattr(repo, "insert_monitor_check", insert)
    monkeypatch.setattr(repo, "mark_monitor_run", mark)
    return seen


async def test_a_check_records_each_page_status_and_the_counts(quiet_repo: dict[str, Any]) -> None:
    svc = _Service(
        {
            "https://a.test/p": {
                "changeStatus": "changed",
                "previousScrapeAt": "2026-09-01T00:00:00Z",
                "diff": "-old\n+new",
                "linesAdded": 1,
                "linesRemoved": 1,
            },
            "https://b.test/q": {
                "changeStatus": "same",
                "previousScrapeAt": "2026-09-01T00:00:00Z",
            },
        }
    )
    out = await mon.run_monitor(_monitor(), svc)
    assert out["counts"] == {"same": 1, "changed": 1, "new": 0, "error": 0}
    changed = next(p for p in out["pages"] if p["url"].endswith("/p"))
    assert changed["status"] == "changed" and changed["diff"] == "-old\n+new"
    assert quiet_repo["checks"][0]["by"] == "schedule"
    assert quiet_repo["runs"] == [("mon_1", 30)], "the schedule advances after the check"


async def test_every_check_page_asks_for_change_tracking_and_never_the_cache(
    quiet_repo: dict[str, Any],
) -> None:
    svc = _Service(
        {"https://a.test/p": {"changeStatus": "new"}, "https://b.test/q": {"changeStatus": "new"}}
    )
    await mon.run_monitor(_monitor(), svc)
    for _, options in svc.calls:
        assert options.change_tracking is not None, "a monitor without change tracking is a scrape"
        assert options.maxAge == 0, "a monitor exists to look again"


async def test_a_dead_url_is_recorded_as_error_and_the_rest_are_still_checked(
    quiet_repo: dict[str, Any],
) -> None:
    svc = _Service(
        {"https://a.test/p": TargetError(404), "https://b.test/q": {"changeStatus": "same"}}
    )
    out = await mon.run_monitor(_monitor(), svc)
    assert out["counts"]["error"] == 1 and out["counts"]["same"] == 1
    err = next(p for p in out["pages"] if p["status"] == "error")
    assert err["error"]["code"] == "TARGET_ERROR"


async def test_the_webhook_fires_only_when_there_is_something_to_say(
    quiet_repo: dict[str, Any], monkeypatch: Any
) -> None:
    sent: list[dict[str, Any]] = []

    async def fake_notify(monitor: Any, payload: Any, key: Any) -> None:
        sent.append(payload)

    monkeypatch.setattr(mon, "_notify", fake_notify)
    quiet = _Service(
        {"https://a.test/p": {"changeStatus": "same"}, "https://b.test/q": {"changeStatus": "same"}}
    )
    await mon.run_monitor(_monitor(webhook_url="https://hook.test/x"), quiet)
    assert sent == [], "all same: nothing to tell anyone"
    loud = _Service(
        {
            "https://a.test/p": {"changeStatus": "changed"},
            "https://b.test/q": {"changeStatus": "same"},
        }
    )
    await mon.run_monitor(_monitor(webhook_url="https://hook.test/x"), loud)
    assert len(sent) == 1 and sent[0]["counts"]["changed"] == 1
    await mon.run_monitor(_monitor(webhook_url=None), loud)
    assert len(sent) == 1, "no webhook configured, none sent"


# --------------------------------------------------------------------------
# The request shape, through the app
# --------------------------------------------------------------------------


def test_interval_floor_and_url_cap_are_enforced_by_the_model() -> None:
    from pydantic import ValidationError

    from engine.api.routes.monitor import MonitorRequest

    with pytest.raises(ValidationError):
        MonitorRequest(name="x", url="https://a.test", intervalMinutes=1)
    with pytest.raises(ValidationError):
        MonitorRequest(name="x", urls=[f"https://a.test/{i}" for i in range(51)])
    req = MonitorRequest(
        name="x", url="https://a.test/", urls=["https://b.test/", "https://a.test/"]
    )
    assert req.all_urls() == ["https://a.test/", "https://b.test/"], "deduplicated, order kept"
    assert MonitorRequest(name="x", url="https://a.test").intervalMinutes == 60


def test_a_monitor_payload_uses_the_api_vocabulary() -> None:
    from datetime import UTC, datetime

    from engine.api.routes.monitor import _payload

    row = _Row(
        id="mon_1",
        name="p",
        urls=["https://a.test"],
        interval_minutes=15,
        goal="g",
        webhook_url=None,
        active=True,
        created_at=datetime(2026, 9, 6, tzinfo=UTC),
        last_run_at=None,
        next_run_at=datetime(2026, 9, 6, 0, 15, tzinfo=UTC),
    )
    out = _payload(row)
    assert out["intervalMinutes"] == 15 and out["nextRunAt"] == "2026-09-06T00:15:00Z"
    assert out["lastRunAt"] is None and "latestCheck" not in out


def test_the_monitor_runner_is_a_scheduled_task_every_minute() -> None:
    from engine.workers.scheduler import TASKS

    task = next(t for t in TASKS if t.name == "monitor_runner")
    assert task.interval_s == 60


def test_the_check_event_name_is_stable() -> None:
    assert mon.CHECK_EVENT == "monitor.check.completed"
    assert json.dumps({"event": mon.CHECK_EVENT})  # serialisable, for the webhook body


async def test_the_trackers_own_key_is_the_one_that_is_read(quiet_repo: dict[str, Any]) -> None:
    """`changeStatus`, as /v1/scrape emits it. The first live run read `status`
    and reported every page new on every check."""
    svc = _Service(
        {
            "https://a.test/p": {"changeStatus": "same"},
            "https://b.test/q": {"changeStatus": "changed"},
        }
    )
    out = await mon.run_monitor(_monitor(), svc)
    assert out["counts"] == {"same": 1, "changed": 1, "new": 0, "error": 0}


async def test_a_key_cannot_hold_more_than_the_cap_of_active_monitors(monkeypatch: Any) -> None:
    """A key that could create ten thousand five-minute monitors is a load
    generator with a bill attached. The cap is enforced BY the insert, in one
    statement, so two concurrent creates cannot both pass a separate count."""
    from engine.api.routes import monitor as routes
    from engine.core.errors import InvalidRequest
    from engine.storage import repositories as repo

    async def refused(*args: Any, **kwargs: Any) -> str | None:
        assert kwargs["cap"] == routes.MAX_MONITORS_PER_KEY, "the cap travels with the insert"
        return None  # the atomic insert wrote nothing: the key is at the cap

    async def resolves(url: str) -> None:
        return None

    monkeypatch.setattr(repo, "create_monitor", refused)
    # The route resolves the URL and checks credits BEFORE counting — right
    # order for a real request, not what this test is about.
    monkeypatch.setattr(routes, "resolve_and_validate", resolves)
    monkeypatch.setattr(routes.billing, "assert_credits", lambda key: None)

    class Key:
        id = "key_1"

    body = routes.MonitorRequest(name="one more", url="https://acmecorp-fixture.io/")
    with pytest.raises(InvalidRequest) as err:
        await routes.create_monitor(body, Key())
    assert "monitors" in err.value.message.lower()
