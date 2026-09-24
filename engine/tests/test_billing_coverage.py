"""Every customer-facing route either bills, or says in writing why it does not.

Metering is write-only: an endpoint that charges nothing still returns correct
pages, still reports a `creditsUsed` figure, and still passes its own tests. The
only witness is the ledger, and nobody reads the ledger to check that a feature
works — so nothing about the endpoint looks wrong.

The route table is therefore the checklist. A new endpoint fails this file until
somebody classifies it, and a route that claims to bill must actually contain a
charge on the path that does the work — which for queued endpoints is the
worker, not the handler.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# route -> the file that must contain the billing.charge for it.
BILLS: dict[str, str] = {
    "/v1/scrape": "engine/api/routes/scrape.py",
    "/v1/map": "engine/api/routes/crawl.py",
    "/v1/extract": "engine/api/routes/extract.py",
    "/v1/search": "engine/api/routes/extract.py",
    "/v1/parse": "engine/api/routes/parse.py",
    "/v1/places/search": "engine/api/routes/places.py",
    # A bought Google results page: flat `serp`, plus `serp_ai_overview` when asked.
    "/v1/serp": "engine/api/routes/serp.py",
    "/v1/posts": "engine/api/routes/platforms.py",
    # RDAP, DNS and our own link graph. Not a fetch — no tier, no proxy, no
    # browser — so it bills a flat `domain` unit rather than a rung.
    "/v1/domain": "engine/api/routes/domain.py",
    # Company enrichment: homepage plus contact/about pages, flat `company` unit.
    "/v1/company": "engine/api/routes/company.py",
    "/v1/products": "engine/api/routes/platforms.py",
    # Queued work: the handler returns a job id, the charge happens per page as
    # the work runs. This split is the trap — such a route looks as billing-free
    # as a status read, because at the handler it is.
    "/v1/crawl": "engine/core/frontier/crawler.py",
    "/v1/batch/scrape": "engine/workers/http_worker.py",
    # Find Leads: charged once when the run finishes, per lead delivered.
    "/v1/leads": "engine/workers/http_worker.py",
    # A check is a scrape on a schedule; the manual run bills the same way.
    "/v1/monitor": "engine/core/monitor.py",
    "/v1/monitor/{monitor_id}/run": "engine/core/monitor.py",
}

# route -> why no charge is correct. Prose, so the reason is reviewable.
EXEMPT: dict[str, str] = {
    "/v1/templates": "a list of field names: no fetch, no model, nothing to bill",
    "/v1/crawl/{job_id}": "status read of work already billed per page",
    "/v1/crawl/{job_id}/pages": "results of work already billed",
    "/v1/crawl/{job_id}/errors": "failures are never charged for",
    "/v1/batch/{job_id}": "status read of work already billed per page",
    "/v1/batch/{job_id}/pages": "results of work already billed",
    "/v1/leads/{job_id}": "status and results of a run billed once when it finished",
    "/v1/monitor/{monitor_id}": "status read; the checks themselves are billed",
    "/v1/monitor/{monitor_id}/checks": "history of checks already billed",
    "/v1/fetch": "operator keys only — a customer key is refused outright",
    "/v1/source": "the AGPL section 13 offer; must be free and unauthenticated",
}


def _v1_routes() -> set[str]:
    from engine.api.app import app

    schema = app.openapi()
    return {
        path
        for path, ops in schema.get("paths", {}).items()
        if path.startswith("/v1/")
        # DELETE-only routes cancel or remove; they produce nothing to bill.
        and set(ops) - {"delete"}
        # The operator desk is not a customer surface.
        and not path.startswith("/v1/internal")
    }


def test_every_v1_route_is_classified_as_billing_or_exempt() -> None:
    """The failure that matters: a NEW endpoint nobody thought about.

    This fires before any coverage test can pass by simply not knowing the
    route exists.
    """
    unclassified = sorted(_v1_routes() - set(BILLS) - set(EXEMPT))
    assert not unclassified, (
        f"new customer-facing route(s) {unclassified} are neither billed nor "
        f"declared exempt. Add a charge and list it in BILLS, or list it in "
        f"EXEMPT with the reason it is free."
    )


def test_no_route_is_declared_both_ways() -> None:
    both = sorted(set(BILLS) & set(EXEMPT))
    assert not both, f"{both} declared as both billing and exempt"


def test_the_classification_names_no_route_that_does_not_exist() -> None:
    """A stale entry makes the coverage above lie by shrinking the gap."""
    live = _v1_routes()
    stale = sorted((set(BILLS) | set(EXEMPT)) - live)
    assert not stale, f"{stale} classified but not routed; remove or fix the path"


@pytest.mark.parametrize("route,where", sorted(BILLS.items()))
def test_a_route_that_bills_has_a_charge_on_the_path_that_does_the_work(
    route: str, where: str
) -> None:
    """Declaring a route billable is not the same as charging for it.

    For queued endpoints the charge is in the worker, so the file named here is
    the one that runs the page; asserting on the handler would pass for any
    queued route whether or not the work is ever charged for.
    """
    assert _calls_charge(ROOT / where), (
        f"{route} is declared billable but {where} never CALLS billing.charge"
    )


def _calls_charge(path: Path) -> bool:
    """A real call node, not the substring.

    Checking `"billing.charge" in source` passed with the call deleted, because
    the phrase survives in a nearby comment — the test rubber-stamped the exact
    bug it exists for. Only an `await billing.charge(...)` counts.
    """
    import ast

    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "charge"
            and isinstance(func.value, ast.Name)
            and func.value.id == "billing"
        ):
            return True
    return False


@pytest.mark.parametrize("reason", sorted(EXEMPT.values()))
def test_every_exemption_states_a_reason(reason: str) -> None:
    assert len(reason) > 15, "an exemption needs a reason somebody can disagree with"
