"""Find Leads for agents: findLeads starts a run, leadsStatus follows it and
reads the leads back 25 at a time. Asked in a conversation ("get me 50 roofers
in Houston with emails"), this is the tool the agent reaches for.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from engine.mcp import server


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # The pipeline is proprietary: the published core has no engine.leads, and
    # these tests are about what the tool does when it does have it.
    pytest.importorskip("engine.leads.models")
    state: dict[str, Any] = {"created": [], "pushed": [], "progress": []}

    async def create_job(kind: str, key_id: str, payload: dict[str, Any], **_: Any) -> str:
        state["created"].append({"kind": kind, "key": key_id, **payload})
        return "leads_1"

    async def set_job_progress(job_id: str, stage: str, total: int, done: int) -> None:
        state["progress"].append((stage, total))

    async def push(message: Any, queue: Any) -> None:
        state["pushed"].append(message.kind)

    async def key_id() -> str:
        return "key_mcp"

    async def price(contacts: bool, roles: int) -> int:
        return 2 + (1 if contacts else 0) + 2 * roles

    from engine.api.routes import leads as route

    monkeypatch.setattr(server.repo, "create_job", create_job)
    monkeypatch.setattr(server.repo, "set_job_progress", set_job_progress)
    monkeypatch.setattr(server._queue, "push", push)
    monkeypatch.setattr(server, "_job_key_id", key_id)
    monkeypatch.setattr(route, "per_lead_price", price)
    return state


@pytest.mark.anyio
async def test_find_leads_starts_a_capped_run_and_says_what_it_costs(jobs: dict[str, Any]) -> None:
    out = await server.findLeads(who="roofing contractors", where="Houston, TX", limit=500)
    created = jobs["created"][0]
    assert created["kind"] == "leads" and created["key"] == "key_mcp"
    assert created["limit"] == server.MAX_AGENT_LEADS
    assert created["sources"] == ["maps", "bbb", "yellowpages"]
    assert jobs["pushed"] == ["leads"]
    assert "leads_1" in out and "leadsStatus" in out
    assert "at most 300 credits" in out and "reduced to 100" in out


@pytest.mark.anyio
async def test_find_leads_refuses_a_bad_search_before_starting(jobs: dict[str, Any]) -> None:
    out = await server.findLeads(who="x", where="Houston")
    assert "could not be started" in out
    assert jobs["created"] == []


def _job(**over: Any) -> dict[str, Any]:
    base = {
        "id": "leads_1",
        "kind": "leads",
        "api_key_id": "key_mcp",
        "status": "completed",
        "stage": "done",
        "completed": 30,
        "total": 30,
        "input": {"who": "roofers", "where": "Houston, TX"},
        "cost": {
            "credits": 88,
            "sources": {"bbb": {"found": 0, "error": "BBB showed no listings"}},
        },
        "error": None,
    }
    return base | over


@pytest.mark.anyio
async def test_leads_status_follows_the_stage_then_pages_the_leads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {"job": _job(status="running", stage="contacts", completed=12, total=30)}

    async def owned(job_id: str) -> Any:
        return current["job"]

    async def results(job_id: str) -> list[dict[str, Any]]:
        return [
            {
                "name": f"Roofer {i}",
                "phone": "(713) 555-0100",
                "website": f"https://roofer{i}.example",
                "emails": [{"email": f"info@roofer{i}.example"}] if i % 2 else [],
                "contactForm": None if i % 2 else f"https://roofer{i}.example/contact",
                "sources": ["maps"],
                "people": [{"name": "Jo Smith"}] if i == 1 else [],
            }
            for i in range(1, 31)
        ]

    monkeypatch.setattr(server, "_owned_job", owned)
    monkeypatch.setattr(server.repo, "list_lead_results", results)

    running = await server.leadsStatus("leads_1")
    assert "12 of 30" in running

    current["job"] = _job()
    first = await server.leadsStatus("leads_1")
    assert "30 leads for roofers in Houston, TX. 88 credits used." in first
    assert "1. Roofer 1 | (713) 555-0100 | info@roofer1.example" in first
    assert "contact: Jo Smith" in first
    assert "2. Roofer 2 | (713) 555-0100 | https://roofer2.example/contact" in first
    assert "25. Roofer 25" in first and "26. Roofer 26" not in first
    assert "offset=25" in first and "BBB showed no listings" in first

    rest = await server.leadsStatus("leads_1", offset=25)
    assert "26. Roofer 26" in rest and "30. Roofer 30" in rest and "offset=" not in rest


@pytest.mark.anyio
async def test_someone_elses_run_does_not_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    async def owned(job_id: str) -> Any:
        return None

    monkeypatch.setattr(server, "_owned_job", owned)
    assert "No Find Leads run" in await server.leadsStatus("leads_other")


@pytest.mark.anyio
async def test_without_the_pipeline_the_tool_says_so_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The published core ships without engine.leads; the tool must answer, not crash."""
    monkeypatch.setitem(sys.modules, "engine.leads.models", None)
    out = await server.findLeads(who="roofing contractors", where="Houston, TX")
    assert out == "Find Leads is not available on this deployment."
