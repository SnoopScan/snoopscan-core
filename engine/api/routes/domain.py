"""POST /v1/domain — what is known about a domain, not a page.

Every other endpoint reads a page. This one reads the things a page cannot
tell you: how old the domain is, who registered it, where it is hosted, and
who links to it. A colleague ran into the wall from the other side on 9 Sep
2026 — "we win every on-page signal and still lose, off-page is the most
likely real gap" — and the answer took one RDAP call: the site outranking us
was registered in 2000.

Nothing here touches the target's web server, so nothing here can be blocked,
rate-limited or fingerprinted, and no proxy is involved. It is priced as one
flat unit for that reason rather than as a fetch.

Backlinks come from OUR OWN crawl graph — every page SnoopScan has ever
fetched, folded down to domain -> domain. That is honest about what it is: it
answers "who, that we have seen, links to this", not "every link on the web".
The response says so in `backlinks.source` rather than leaving a caller to
assume a web-scale index.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from engine.api import billing
from engine.api.deps import ApiKeyDep
from engine.core import domain_intel
from engine.core.models import Cost, DomainRequest
from engine.storage import repositories as repo

router = APIRouter(tags=["domain"])


@router.post("/domain")
async def domain_intelligence(body: DomainRequest, key: ApiKeyDep) -> dict[str, Any]:
    billing.assert_credits(key)

    host = domain_intel.normalise(body.domain)

    registration, dns, links = await asyncio.gather(
        domain_intel.registration(host) if body.registration else _none(),
        domain_intel.dns_records(host) if body.dns else _none(),
        _backlinks(host, body.backlinkLimit) if body.backlinks else _none(),
    )

    cost = Cost(extras={"domain": 1})
    await billing.charge(key, endpoint="domain", url=host, cost=cost)

    data: dict[str, Any] = {"domain": host, "cost": cost.model_dump()}
    if registration is not None:
        data["registration"] = vars(registration)
    elif body.registration:
        # Asked for and not delivered. A null with no reason is the fault this
        # engine has been chasing all week.
        data["registration"] = None
        data.setdefault("warnings", []).append(
            "registration: no RDAP record for this domain — it may be "
            "unregistered, or its registry may not publish RDAP."
        )
    if dns is not None:
        data["dns"] = vars(dns)
    if links is not None:
        data["backlinks"] = links

    return {"success": True, "data": data}


async def _none() -> None:
    return None


async def _backlinks(host: str, limit: int) -> dict[str, Any]:
    referring, seen = await repo.backlink_totals(host)
    out_domains, out_links = await repo.outbound_totals(host)
    rows = await repo.backlinks(host, limit=limit) if referring else []

    return {
        # Named, not implied. This is what SnoopScan has crawled, which is not
        # the web — a caller comparing it against a backlink vendor's number
        # deserves to know why they differ.
        "source": "snoopscan-crawl",
        "referringDomains": referring,
        "linksSeen": seen,
        "outboundDomains": out_domains,
        "outboundLinks": out_links,
        "referrers": [
            {
                "domain": r["source_domain"],
                "links": r["links"],
                "sampleFrom": r["sample_source_url"],
                "sampleTo": r["sample_target_url"],
                "firstSeen": r["first_seen"].isoformat() if r["first_seen"] else None,
                "lastSeen": r["last_seen"].isoformat() if r["last_seen"] else None,
            }
            for r in rows
        ],
    }
