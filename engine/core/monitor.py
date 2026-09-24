"""Running a monitor: every URL through change tracking, one check row, one
webhook. The comparison is /v1/scrape's own; this only decides when and tells
someone.

Statuses per page, in Firecrawl's vocabulary because the option names are
migration-compatible on purpose: `same`, `changed`, `new` (first capture),
`error`. A check that could not fetch a page records the error and carries on —
one dead URL must not stop the other nine being compared.
"""

from __future__ import annotations

from typing import Any

import structlog

from engine.api import billing
from engine.core.errors import EngineError
from engine.core.models import ScrapeOptions
from engine.storage import repositories as repo

logger = structlog.get_logger(__name__)

CHECK_EVENT = "monitor.check.completed"
MIN_INTERVAL_MINUTES = 5
MAX_URLS = 50


def check_options() -> ScrapeOptions:
    return ScrapeOptions(
        formats=["markdown", {"type": "changeTracking", "modes": ["git-diff"]}],
        maxAge=0,  # a monitor exists to look again; the cache is the enemy here
    )


async def check_page(service: Any, url: str, key: Any) -> dict[str, Any]:
    try:
        outcome = await service.scrape(url, check_options())
    except EngineError as exc:
        return {
            "url": url,
            "status": "error",
            "error": {"code": str(exc.code), "message": exc.message},
        }
    except Exception as exc:  # noqa: BLE001 - recorded on the page, never raised
        return {
            "url": url,
            "status": "error",
            "error": {"code": "INTERNAL", "message": str(exc)[:200]},
        }
    if key is not None:
        try:
            await billing.charge(key, endpoint="monitor", url=url, cost=outcome.data.cost)
        except Exception as exc:  # noqa: BLE001 - logged; a charge that fails is not a failed check
            logger.warning("monitor_charge_failed", url=url, error=str(exc))
    tracking = outcome.data.changeTracking or {}
    # The tracker speaks Firecrawl's vocabulary: the key is `changeStatus`.
    # Reading `status` made every page "new" on every check (6 Sep 2026).
    status = tracking.get("changeStatus") or tracking.get("status") or "new"
    page: dict[str, Any] = {"url": url, "status": status}
    if status == "changed":
        for k in ("previousScrapeAt", "linesAdded", "linesRemoved", "added", "removed"):
            if k in tracking:
                page[k] = tracking[k]
        if tracking.get("diff"):
            page["diff"] = tracking["diff"][:20_000]
    elif "previousScrapeAt" in tracking:
        page["previousScrapeAt"] = tracking["previousScrapeAt"]
    return page


async def run_monitor(
    monitor: Any, service: Any, *, triggered_by: str = "schedule"
) -> dict[str, Any]:
    """One check. Returns the check payload that was stored (and sent)."""
    key = await repo.api_key_by_id(monitor["api_key_id"])
    urls = list(monitor["urls"] or [])[:MAX_URLS]
    pages = [await check_page(service, url, key) for url in urls]
    counts: dict[str, int] = {}
    for p in pages:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    check_id = await repo.insert_monitor_check(
        monitor["id"], pages=pages, counts=counts, triggered_by=triggered_by
    )
    await repo.mark_monitor_run(monitor["id"], int(monitor["interval_minutes"]))
    payload = {
        "id": check_id,
        "monitorId": monitor["id"],
        "name": monitor["name"],
        "triggeredBy": triggered_by,
        "counts": {k: counts.get(k, 0) for k in ("same", "changed", "new", "error")},
        "pages": pages,
    }
    logger.info("monitor_check", monitor=monitor["id"], **payload["counts"])
    notable = counts.get("changed") or counts.get("new") or counts.get("error")
    if monitor["webhook_url"] and notable:
        await _notify(monitor, payload, key)
    return payload


async def _notify(monitor: Any, payload: dict[str, Any], key: Any) -> None:
    from engine.core.politeness import get_redis
    from engine.core.webhooks import WebhookEvent, WebhookSender, send_once

    try:
        redis = await get_redis()
        await send_once(
            WebhookSender(),
            redis,
            monitor["webhook_url"],
            WebhookEvent(
                event=CHECK_EVENT, job_id=monitor["id"], data=payload, page_id=payload["id"]
            ),
            secret=getattr(key, "webhook_secret", None),
        )
    except Exception as exc:  # noqa: BLE001 - a webhook that fails is logged; the check stands
        logger.warning("monitor_webhook_failed", monitor=monitor["id"], error=str(exc))


async def run_due(service: Any) -> int:
    """Every monitor whose time has come. Called by the scheduler each minute."""
    due = await repo.due_monitors()
    for monitor in due:
        try:
            await run_monitor(monitor, service)
        except Exception as exc:  # noqa: BLE001 - one monitor's failure is not another's
            logger.error("monitor_run_failed", monitor=monitor["id"], error=str(exc))
            await repo.mark_monitor_run(monitor["id"], int(monitor["interval_minutes"]))
    return len(due)
