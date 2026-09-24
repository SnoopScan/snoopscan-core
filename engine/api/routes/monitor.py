"""Monitors — watch pages for changes on a schedule (01-api-surface.md).

POST   /v1/monitor                create
GET    /v1/monitor                list this key's monitors
GET    /v1/monitor/{id}           one monitor with its latest check
DELETE /v1/monitor/{id}           delete (checks go with it)
POST   /v1/monitor/{id}/run       check now, synchronously
GET    /v1/monitor/{id}/checks    recent checks
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.api import billing
from engine.api.deps import ApiKeyDep, ServiceDep
from engine.core.errors import InvalidRequest, JobNotFound
from engine.core.monitor import MAX_URLS, MIN_INTERVAL_MINUTES, run_monitor
from engine.core.ssrf import resolve_and_validate
from engine.storage import repositories as repo

router = APIRouter(tags=["monitor"])

# A key that could create ten thousand five-minute monitors is a load generator
# with a bill attached. Generous for a real customer; a wall for a mistake.
MAX_MONITORS_PER_KEY = 100


class MonitorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, max_length=120, description="A name for the monitor, up to 120 characters."
    )
    url: str | None = Field(
        default=None, description="A page to watch. Give this, `urls`, or both."
    )
    urls: list[str] = Field(
        default_factory=list, description="Pages to watch in the one monitor, up to 50."
    )
    intervalMinutes: int = Field(
        default=60,
        ge=MIN_INTERVAL_MINUTES,
        le=7 * 24 * 60,
        description="How often to check, in minutes: at least 5, at most a week (10,080).",
    )
    goal: str | None = Field(
        default=None,
        max_length=1000,
        description=(
            "What you are watching for, in your own words. Stored and returned with the monitor; "
            "it does not yet affect which changes are reported."
        ),
    )
    webhook: str | None = Field(
        default=None,
        description="A URL to POST to when a check finds a page changed, new or failing.",
    )

    @field_validator("urls")
    @classmethod
    def _cap(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_URLS:
            raise ValueError(f"at most {MAX_URLS} urls per monitor")
        return v

    def all_urls(self) -> list[str]:
        urls = ([self.url] if self.url else []) + list(self.urls)
        return list(dict.fromkeys(u.strip() for u in urls if u and u.strip()))


def _payload(row: Any, latest: Any | None = None) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "name": row["name"],
        "urls": list(row["urls"] or []),
        "intervalMinutes": row["interval_minutes"],
        "goal": row["goal"],
        "webhook": row["webhook_url"],
        "active": row["active"],
        "createdAt": _iso(row["created_at"]),
        "lastRunAt": _iso(row["last_run_at"]),
        "nextRunAt": _iso(row["next_run_at"]),
    }
    if latest is not None:
        out["latestCheck"] = _check_payload(latest)
    return out


def _check_payload(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "startedAt": _iso(row["started_at"]),
        "finishedAt": _iso(row["finished_at"]),
        "triggeredBy": row["triggered_by"],
        "counts": {
            "same": row["same"],
            "changed": row["changed"],
            "new": row["new"],
            "error": row["errors"],
        },
        "pages": list(row["pages"] or []),
    }


def _iso(value: Any) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


async def _load(monitor_id: str, key_id: str) -> Any:
    row = await repo.get_monitor(monitor_id)
    # Unknown and someone else's are both 404: confirming existence leaks.
    if row is None or row["api_key_id"] != key_id:
        raise JobNotFound(monitor_id)
    return row


@router.post("/monitor")
async def create_monitor(body: MonitorRequest, key: ApiKeyDep) -> dict[str, Any]:
    urls = body.all_urls()
    if not urls:
        raise InvalidRequest("A monitor needs a url or urls")
    for url in urls:
        await resolve_and_validate(url)
    billing.assert_credits(key)
    # The cap is enforced by the insert itself, not by a count beforehand: two
    # creates racing a separate count could both pass it.
    monitor_id = await repo.create_monitor(
        key.id,
        name=body.name,
        urls=urls,
        interval_minutes=body.intervalMinutes,
        goal=body.goal,
        webhook_url=body.webhook,
        cap=MAX_MONITORS_PER_KEY,
    )
    if monitor_id is None:
        raise InvalidRequest(
            f"This key already has {MAX_MONITORS_PER_KEY} active monitors; delete one first",
            {"limit": MAX_MONITORS_PER_KEY},
        )
    row = await repo.get_monitor(monitor_id)
    return {"success": True, "data": _payload(row)}


@router.get("/monitor")
async def list_monitors(key: ApiKeyDep) -> dict[str, Any]:
    rows = await repo.list_monitors(key.id)
    return {"success": True, "data": {"monitors": [_payload(r) for r in rows]}}


@router.get("/monitor/{monitor_id}")
async def get_monitor(monitor_id: str, key: ApiKeyDep) -> dict[str, Any]:
    row = await _load(monitor_id, key.id)
    checks = await repo.list_monitor_checks(monitor_id, limit=1)
    return {"success": True, "data": _payload(row, checks[0] if checks else None)}


@router.delete("/monitor/{monitor_id}")
async def delete_monitor(monitor_id: str, key: ApiKeyDep) -> dict[str, Any]:
    await _load(monitor_id, key.id)
    await repo.delete_monitor(monitor_id)
    return {"success": True, "data": {"id": monitor_id, "deleted": True}}


@router.post("/monitor/{monitor_id}/run")
async def run_now(monitor_id: str, key: ApiKeyDep, service: ServiceDep) -> dict[str, Any]:
    """A check right now, in the request. Billed like the scheduled ones."""
    row = await _load(monitor_id, key.id)
    billing.assert_credits(key)
    check = await run_monitor(row, service, triggered_by="manual")
    return {"success": True, "data": check}


@router.get("/monitor/{monitor_id}/checks")
async def list_checks(
    monitor_id: str, key: ApiKeyDep, limit: int = Query(default=20, ge=1, le=100)
) -> dict[str, Any]:
    await _load(monitor_id, key.id)
    rows = await repo.list_monitor_checks(monitor_id, limit=limit)
    return {"success": True, "data": {"checks": [_check_payload(r) for r in rows]}}
