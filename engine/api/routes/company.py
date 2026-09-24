"""POST /v1/company — everything a company's own site says about itself.

The contract is public; the source behind it is not. Contact discovery and
firmographics sit behind `engine.leadgen` on the proprietary side of the split
(WITHHELD: "an internal business, not part of the product"), so they are
imported at call time and their absence is a 503 with a reason — the same
shape as Places, and for the same reason: nothing is broken, this deployment
simply does not ship it.

This is the SINGLE-company half of the lead pipeline. The 70-directory
ingestion that turns a whole market into a list stays an internal CLI; what a
customer can call is "enrich this one domain".
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from engine.api import billing
from engine.api.deps import ApiKeyDep, ServiceDep
from engine.core import domain_intel
from engine.core.errors import CompanyUnavailable
from engine.core.models import CompanyRequest, Cost

router = APIRouter(tags=["company"])


def _load() -> Any:
    """The discovery + firmographics entry points, or CompanyUnavailable."""
    try:
        from engine.leadgen.discovery import DiscoveryStatus, discover_contacts
        from engine.leadgen.firmographics import from_html
    except ImportError as exc:  # open core: the module is not shipped
        raise CompanyUnavailable() from exc
    return discover_contacts, from_html, DiscoveryStatus


@router.post("/company")
async def company(body: CompanyRequest, key: ApiKeyDep, service: ServiceDep) -> dict[str, Any]:
    discover_contacts, from_html, DiscoveryStatus = _load()
    billing.assert_credits(key)

    host = domain_intel.normalise(body.url)
    url = body.url if body.url.startswith(("http://", "https://")) else f"https://{host}"

    from engine.core.errors import EngineError, ErrorCode
    from engine.core.models import ScrapeOptions

    # The homepage carries the firmographics (JSON-LD, microdata, Open Graph).
    # `only_main_content=False` keeps the footer, where the address, the company
    # number and the social links usually live.
    #
    # NO tier cap. This was pinned to tier 0/1 on the grounds that a company
    # needing a browser is "a poor lead and a dear fetch" — which decided, on
    # the customer's behalf, that the answer they are paying for is not worth
    # five credits. It is the lead-enrichment endpoint: the data IS the
    # product, and a thin result plus a warning is the failure mode that loses
    # the account. Climb as far as the page needs; the real cost comes back on
    # the response, and a caller who wants a ceiling passes `maxTier`.
    #
    # A homepage that will not read at all is still not a hard failure — the
    # contact pass may find emails and socials, and half a lead beats an error.
    firmographics = from_html("", url)
    warnings: list[str] = []
    home_error: EngineError | None = None
    try:
        home = await service.scrape(
            url,
            ScrapeOptions(formats=["markdown", "html"], onlyMainContent=False),
            owner_ref=key.owner_ref,
        )
        firmographics = from_html(home.data.html or "", url)
    except EngineError as exc:
        # A domain that does not exist is the caller's to fix and worth raising;
        # a site that will not read at any rung degrades to a warning.
        if exc.code == ErrorCode.FETCH_FAILED:
            raise
        home_error = exc
        warnings.append(
            "The homepage could not be read, so firmographics may be thin. "
            "Contact discovery was still attempted."
        )

    pages_fetched = 1
    emails: list[dict[str, Any]] = []
    socials: dict[str, str] = {}
    contact_form: str | None = None
    jurisdiction: str | None = None

    if body.contacts:
        found = await discover_contacts(url, service)
        pages_fetched = max(pages_fetched, found.pages_fetched)
        jurisdiction = found.jurisdiction
        contact_form = found.contact_form_url
        socials = found.social_links
        emails = [
            {
                "email": e.address,
                "role": e.is_role_account,
                "freemail": e.is_freemail,
                "onDomain": e.matches_company_domain,
                "source": str(e.source),
            }
            for e in found.emails
        ]

    # Nothing to sell — no firmographics, no contacts — and nothing readable
    # underneath: this is a failure, not a thin lead, so it is not charged.
    got_anything = firmographics.filled() > 0 or emails or socials or contact_form
    if not got_anything and home_error is not None:
        raise home_error

    cost = Cost(extras={"company": 1})
    await billing.charge(key, endpoint="company", url=url, cost=cost)

    data = {
        "domain": host,
        "company": {
            "name": firmographics.name,
            "description": firmographics.description,
            "phone": firmographics.phone,
            "address": {
                "street": firmographics.street,
                "city": firmographics.city,
                "region": firmographics.region,
                "postalCode": firmographics.postal_code,
                "country": firmographics.country,
            },
            "linkedin": firmographics.linkedin_url,
            "headcount": firmographics.headcount,
            "industry": firmographics.industry,
            "foundedYear": firmographics.founded_year,
            "revenue": firmographics.revenue_raw,
        },
        "people": [
            {"name": p.full_name, "title": p.title, "email": p.email, "linkedin": p.linkedin_url}
            for p in firmographics.people
        ],
        "contacts": {
            "emails": emails,
            "contactForm": contact_form,
            "social": socials,
            "jurisdiction": jurisdiction,
        },
        "pagesRead": pages_fetched,
        "cost": cost.model_dump(exclude_none=True),
    }
    if warnings:
        data["warnings"] = warnings
    return {"success": True, "data": data}
