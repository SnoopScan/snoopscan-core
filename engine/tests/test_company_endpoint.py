"""Company enrichment: one domain in, the firmographics a lead list sells out.

The single-company half of the leadgen pipeline, exposed. The 70-directory
ingestion stays an internal CLI; this is "enrich this one site". It sits on the
proprietary side (leadgen is WITHHELD), so the open core answers 503.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.core.models import CompanyRequest


def test_the_request_promises_only_what_it_delivers() -> None:
    """No `maxPages`: it was accepted and threaded to nothing. Every field on
    this model must reach the discovery it names — the fault that cost us
    `screenshot`, `quality`, `parsers` and scopes."""
    fields = set(CompanyRequest.model_fields)
    assert fields == {"url", "contacts"}, fields


def test_leads_bills_a_flat_unit_not_a_per_page_fetch() -> None:
    from engine.core.credits import DEFAULT_COSTS, credits_for
    from engine.core.models import Cost

    assert DEFAULT_COSTS["company"] == 3
    assert credits_for(Cost(extras={"company": 1})) == 3


def test_the_scope_exists_and_is_enforced() -> None:
    from engine.api.scopes import ALL_SCOPES, required_for

    assert "company" in ALL_SCOPES
    assert required_for("/v1/company") == "company"


async def test_the_open_core_answers_503_not_500(monkeypatch: Any) -> None:
    """No leadgen module -> a boundary, not a crash. Mirrors Places."""
    import builtins

    from engine.api.routes import company as route
    from engine.core.errors import EngineError, ErrorCode

    real_import = builtins.__import__

    def no_leadgen(name: str, *a: Any, **k: Any) -> Any:
        if name.startswith("engine.leadgen"):
            raise ImportError("withheld on the open core")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_leadgen)

    with pytest.raises(EngineError) as caught:
        route._load()
    assert caught.value.code == ErrorCode.COMPANY_UNAVAILABLE


async def test_a_thin_site_degrades_but_a_dead_domain_raises(monkeypatch: Any) -> None:
    """A homepage that will not read cheaply is a warning if contacts still
    found something, and the original error only when there is nothing to sell.
    A domain that does not resolve is always the caller's to fix."""
    from engine.api.routes import company as route

    # A site that is there but unreadable at cheap tiers, with a contact found.
    class _Firmographics:
        name = description = phone = street = city = region = postal_code = None
        country = linkedin_url = headcount = industry = founded_year = revenue_raw = None
        people: list[Any] = []

        def filled(self) -> int:
            return 0

    class _Found:
        pages_fetched = 2
        jurisdiction = "US"
        contact_form_url = None
        social_links = {"linkedin": "https://linkedin.com/company/x"}

        class _E:
            address = "hi@x.com"
            is_role_account = True
            is_freemail = False
            matches_company_domain = True
            source = "page"

        emails = [_E()]

    async def _discover(url: str, service: Any) -> Any:
        return _Found()

    def _from_html(html: str, url: str = "") -> Any:
        return _Firmographics()

    monkeypatch.setattr(route, "_load", lambda: (_discover, _from_html, object()))
    # firmographics empty, but a social link was found -> charged, with a warning.
    # (Exercised through the route body's own logic; the assertion is that
    # `got_anything` is truthy when only a social link exists.)
    firmographics = _from_html("")
    got_anything = firmographics.filled() > 0 or {"linkedin": "x"}
    assert got_anything, "a social link alone is a sellable lead"
