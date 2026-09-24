"""POST /v1/leads and GET /v1/leads/{id} — Find Leads. Asynchronous.

Businesses by what they do and where, merged across directories, each with how
to reach it. A run takes minutes, so it is a job: start it, then read it back.
Charged once, at the end, per lead DELIVERED — never per fetch behind it.

The contract is public; the finding is not. `engine.leads` sits on the
proprietary side of the split, imported at call time, and its absence is a 503
with a reason, the same shape as Places.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import ValidationError

from engine.api import billing
from engine.api.deps import ApiKeyDep
from engine.core import credits as rules
from engine.core.errors import InvalidRequest, JobNotFound, PlacesUnavailable
from engine.core.models import Cost, JobStatus
from engine.storage import repositories as repo
from engine.workers.queue import JobMessage, JobQueue, Queue

router = APIRouter(tags=["leads"])
_queue = JobQueue()


def _validated(body: dict[str, Any]) -> Any:
    try:
        from engine.leads.models import LeadsRequest
    except ImportError as exc:
        raise PlacesUnavailable() from exc
    try:
        return LeadsRequest.model_validate(body)
    except ValidationError as exc:
        problems = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ())),
                "message": err.get("msg", "invalid"),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        raise InvalidRequest("Request failed validation", {"problems": problems}) from exc


async def per_lead_price(contacts: bool, roles: int) -> int:
    """The most one lead can cost: contacts and people are charged only when
    found, so this is a ceiling, and it is what the balance is checked against."""
    extras = {"lead": 1, "lead_contacts": 1 if contacts else 0, "lead_person": roles}
    return rules.credits_for(Cost(extras=extras), await billing.cost_table())


@router.post("/leads")
async def start_leads(body: dict[str, Any], key: ApiKeyDep) -> dict[str, Any]:
    req = _validated(body)
    billing.assert_credits(key)
    per_lead = await per_lead_price(req.contacts, len(req.roles))
    # What the balance covers, as crawl does: lowered and said so, not accepted
    # in full and cut off halfway.
    allowed = await billing.affordable_limit(key, req.limit, per_page=per_lead)

    payload = req.model_dump(mode="json")
    payload["limit"] = allowed
    job_id = await repo.create_job("leads", key.id, payload)
    await repo.set_job_progress(job_id, "queued", allowed, 0)
    await _queue.push(JobMessage(job_id=job_id, kind="leads"), Queue.FETCH_HTTP)
    return {
        "success": True,
        "data": {
            "id": job_id,
            "status": str(JobStatus.QUEUED),
            "limit": allowed,
            "limitRequested": req.limit,
            "creditsPerLeadMax": per_lead,
            "creditsMax": allowed * per_lead,
        },
    }


@router.get("/leads/{job_id}")
async def leads_status(job_id: str, key: ApiKeyDep) -> dict[str, Any]:
    job = await repo.get_job(job_id)
    # Someone else's job is as unknown as a missing one.
    if job is None or job["api_key_id"] != key.id or job["kind"] != "leads":
        raise JobNotFound(job_id)
    cost = job["cost"] or {}
    done = job["status"] == str(JobStatus.COMPLETED)
    leads = await repo.list_lead_results(job_id) if done else []
    return {
        "success": True,
        "data": {
            "id": job_id,
            "status": job["status"],
            # queued, finding, contacts, done: what it is doing right now.
            "stage": job["stage"],
            "progress": {"done": job["completed"], "of": job["total"]},
            "request": job["input"],
            "leads": leads,
            "summary": {
                "leads": len(leads),
                "withPhone": sum(1 for x in leads if x.get("phone")),
                "withEmail": sum(1 for x in leads if x.get("emails")),
                "withPerson": sum(1 for x in leads if x.get("people")),
                "listingsFound": cost.get("listings", 0),
                "duplicatesMerged": cost.get("merged", 0),
                "sources": cost.get("sources", {}),
            },
            "creditsUsed": cost.get("credits", 0),
            "error": job["error"],
        },
    }
