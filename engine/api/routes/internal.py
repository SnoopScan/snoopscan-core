"""/internal — the operator app's API. Not for customers; guarded by a shared token.

The app owns customers, plans and the ledger. The engine owns keys (it must
enforce them at request time), balances (it decrements them) and usage (it
meters it). This surface is how the two agree.
"""

from __future__ import annotations

import secrets
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from engine.api.deps import InternalDep
from engine.core.errors import EngineError, ErrorCode, InvalidRequest, JobNotFound
from engine.storage import repositories as repo

router = APIRouter(prefix="/internal", tags=["internal"])


class CreateKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_ref: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=80)
    scopes: list[str] | None = None
    rate_limit_rpm: int = Field(default=60, ge=1, le=10_000)
    allow_js_exec: bool = False


class VerifyKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=8, max_length=256)


class UpdateKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, min_length=1, max_length=80)
    active: bool | None = None
    rate_limit_rpm: int | None = Field(default=None, ge=1, le=10_000)
    allow_js_exec: bool | None = None
    # The repository has always been able to write this column; only this
    # model left it out, so a key minted with the wrong scopes could never be
    # corrected — it had to be revoked and re-issued, which changes the secret
    # a customer has already pasted into their code. Measured 15 Sep 2026:
    # four live Playground keys stuck on scrape/crawl/map with no way back.
    scopes: list[str] | None = None


class UpdateOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    concurrency: int | None = Field(default=None, ge=1, le=1_000)
    suspended: bool | None = None


class CreditDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delta: int = Field(ge=-1_000_000_000, le=1_000_000_000)


class CreditCosts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    costs: dict[str, int]


@router.get("/health")
async def health(_: InternalDep) -> dict[str, Any]:
    return {"success": True, "data": await repo.health_summary()}


@router.get("/keys")
async def keys(_: InternalDep, owner_ref: str | None = None) -> dict[str, Any]:
    return {"success": True, "data": await repo.list_api_keys(owner_ref)}


@router.post("/keys", status_code=201)
async def create_key(body: CreateKey, _: InternalDep) -> dict[str, Any]:
    plaintext = f"sk_{secrets.token_urlsafe(32)}"
    webhook_secret = secrets.token_urlsafe(32)
    key_id = await repo.create_api_key(
        plaintext,
        body.label,
        scopes=body.scopes,
        rate_limit_rpm=body.rate_limit_rpm,
        allow_js_exec=body.allow_js_exec,
        webhook_secret=webhook_secret,
        owner_ref=body.owner_ref,
    )
    # The plaintext is returned exactly once and stored nowhere. The webhook
    # signing secret is returned the same once: the engine keeps it to sign
    # deliveries, but no list endpoint ever reads it back.
    return {
        "success": True,
        "data": {
            "id": key_id,
            "key": plaintext,
            "prefix": plaintext[:11],
            "webhook_secret": webhook_secret,
        },
    }


@router.post("/keys/verify")
async def verify_key(body: VerifyKey, _: InternalDep) -> dict[str, Any]:
    """Who does this plaintext key belong to? The desk's own API authenticates
    customers with the same keys the engine does, and this is how it asks —
    the plaintext never lands in the desk's database. An inactive or unknown
    key is a 404, never a hint."""
    key = await repo.get_api_key(body.key)
    if key is None or not key.active:
        raise EngineError(ErrorCode.JOB_NOT_FOUND, "Unknown key.", {"reason": "key_not_found"})
    return {
        "success": True,
        "data": {
            "id": key.id,
            "owner_ref": key.owner_ref,
            "label": key.label,
            "scopes": key.scopes,
            "prefix": body.key[:11],
            "suspended": key.suspended,
        },
    }


@router.patch("/keys/{key_id}")
async def update_key(key_id: str, body: UpdateKey, _: InternalDep) -> dict[str, Any]:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise InvalidRequest("Nothing to update")
    if not await repo.update_api_key(key_id, **fields):
        raise JobNotFound(key_id)
    return {"success": True}


@router.get("/owners/{owner_ref}")
async def owner(owner_ref: str, _: InternalDep) -> dict[str, Any]:
    data = await repo.get_owner(owner_ref)
    if data is None:
        raise JobNotFound(owner_ref)
    return {"success": True, "data": data}


@router.patch("/owners/{owner_ref}")
async def update_owner(owner_ref: str, body: UpdateOwner, _: InternalDep) -> dict[str, Any]:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise InvalidRequest("Nothing to update")
    await repo.update_owner(owner_ref, **fields)
    return {"success": True, "data": await repo.get_owner(owner_ref)}


@router.post("/owners/{owner_ref}/credits")
async def credits(owner_ref: str, body: CreditDelta, _: InternalDep) -> dict[str, Any]:
    """Grant or claw back. The balance is the owner's, shared by all their keys."""
    balance = await repo.adjust_credits(owner_ref, body.delta)
    return {"success": True, "data": {"owner_ref": owner_ref, "credits_remaining": balance}}


@router.get("/usage")
async def usage(
    _: InternalDep,
    owner_ref: str | None = None,
    key_id: str | None = None,
    days: int = Query(default=30, ge=1, le=366),
) -> dict[str, Any]:
    if not owner_ref and not key_id:
        raise InvalidRequest("owner_ref or key_id is required")
    return {
        "success": True,
        "data": await repo.usage_summary(owner_ref=owner_ref, key_id=key_id, days=days),
    }


@router.get("/events")
async def events(
    _: InternalDep,
    owner_ref: str | None = None,
    key_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    if not owner_ref and not key_id:
        raise InvalidRequest("owner_ref or key_id is required")
    data = await repo.list_usage_events(owner_ref=owner_ref, key_id=key_id, limit=limit)
    return {"success": True, "data": data}


@router.get("/credit-costs")
async def get_costs(_: InternalDep) -> dict[str, Any]:
    return {"success": True, "data": await repo.get_credit_costs()}


@router.put("/credit-costs")
async def put_costs(body: CreditCosts, _: InternalDep) -> dict[str, Any]:
    bad = [k for k, v in body.costs.items() if v < 0 or v > 1000]
    if bad:
        raise InvalidRequest("credits must be 0..1000", {"keys": bad})
    await repo.set_credit_costs(body.costs)
    from engine.api import billing

    billing.invalidate_cost_table()
    return {"success": True, "data": await repo.get_credit_costs()}


@router.get("/rate-limited")
async def rate_limited(
    _: InternalDep,
    day: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict[str, Any]:
    """Keys refused with a 429 for going over their per-minute limit on one UTC
    day (default today), as [{key_id, refused}], busiest first. Read-only; the
    counts are kept three days. The app emails an owner whose key keeps
    hitting its limit (RateLimitHit)."""
    from datetime import UTC, datetime

    from engine.core.politeness import RateLimiter

    wanted = day or datetime.now(UTC).strftime("%Y-%m-%d")
    return {"success": True, "data": await RateLimiter().refused_on(wanted)}


@router.get("/jobs")
async def jobs(
    _: InternalDep, limit: int = Query(default=50, ge=1, le=500), owner_ref: str | None = None
) -> dict[str, Any]:
    return {"success": True, "data": await repo.list_jobs(limit, owner_ref)}


@router.get("/domains")
async def domains(
    _: InternalDep,
    limit: int = Query(default=100, ge=1, le=1000),
    sort: str = "recent",
    q: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    return {"success": True, "data": await repo.list_domain_profiles(limit, sort, q)}


class TemplateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_: dict[str, Any] = Field(alias="schema")
    description: str = Field(default="", max_length=200)
    active: bool = True


@router.get("/templates")
async def list_templates(_: InternalDep) -> dict[str, Any]:
    """Every template in force, shipped and desk-managed alike."""
    from engine.core.extract import templates as _templates

    return {"success": True, "data": _templates.catalogue()}


@router.put("/templates/{name}")
async def put_template(name: str, body: TemplateBody, _: InternalDep) -> dict[str, Any]:
    """Add a template, or override a shipped one by using its name."""
    from engine.core.extract import templates as _templates

    if body.schema_.get("type") not in (None, "object"):
        raise InvalidRequest("A template's schema must describe an object.")
    row = await repo.upsert_extraction_template(
        name.strip(), body.schema_, body.description, body.active
    )
    await _templates.refresh()
    return {"success": True, "data": row}


@router.delete("/templates/{name}")
async def delete_template(name: str, _: InternalDep) -> dict[str, Any]:
    """Remove a desk template. A shipped one of the same name comes back."""
    from engine.core.extract import templates as _templates

    if not await repo.delete_extraction_template(name):
        raise JobNotFound(name)
    await _templates.refresh()
    return {"success": True}


@router.post("/domains/{domain}/reset-circuit")
async def reset_domain_circuit(domain: str, _: InternalDep) -> dict[str, Any]:
    """Clear one domain's lockout, keeping everything else learned about it."""
    if not await repo.reset_domain_circuit(domain.strip().lower()):
        raise JobNotFound(domain)
    return {"success": True, "data": {"domain": domain, "circuit": "closed"}}


@router.get("/proxies")
async def proxies(_: InternalDep) -> dict[str, Any]:
    return {"success": True, "data": await repo.list_proxies()}


class CreateProvider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    type: Literal["datacenter", "isp", "residential", "mobile"]
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=255)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    username_template: str | None = Field(default=None, max_length=255)
    password_template: str | None = Field(default=None, max_length=255)
    password_sticky_template: str | None = Field(default=None, max_length=255)
    sticky_lifetime_minutes: int | None = Field(default=None, ge=1, le=1440)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10_000)
    # budget: cheap by definition, first among equal prices. premium: the
    # grade hard work (a known firewall, clicking, a phone) is narrowed to.
    grade: Literal["budget", "premium"] | None = None
    # USD per GB, as the vendor bills it. Within a priority the cheapest
    # healthy provider takes the request; unset, the type's estimate stands in.
    cost_per_gb: float | None = Field(default=None, ge=0, le=1_000)


class UpdateProvider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    type: Literal["datacenter", "isp", "residential", "mobile"] | None = None
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=255)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    username_template: str | None = Field(default=None, max_length=255)
    password_template: str | None = Field(default=None, max_length=255)
    password_sticky_template: str | None = Field(default=None, max_length=255)
    sticky_lifetime_minutes: int | None = Field(default=None, ge=1, le=1440)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10_000)
    # budget: cheap by definition, first among equal prices. premium: the
    # grade hard work (a known firewall, clicking, a phone) is narrowed to.
    grade: Literal["budget", "premium"] | None = None
    # USD per GB, as the vendor bills it. Within a priority the cheapest
    # healthy provider takes the request; unset, the type's estimate stands in.
    cost_per_gb: float | None = Field(default=None, ge=0, le=1_000)


def _providers_changed() -> None:
    try:
        from engine.core.proxy import providers

        providers.invalidate()
    except ImportError:  # the open core ships without the proxy layer
        pass


@router.get("/proxy-providers")
async def proxy_providers(_: InternalDep) -> dict[str, Any]:
    """The registry, plus which providers are currently benched.

    Without the health view, a provider quietly stepping aside looks like
    nothing at all from the desk — traffic moves to another vendor and the bill
    follows it, with no visible reason.
    """
    from engine.core.proxy import providers as _providers

    rows = await repo.list_proxy_providers()
    health = _providers.health_snapshot()
    for row in rows:
        row["health"] = health.get(
            row["id"], {"consecutiveFailures": 0, "benched": False, "secondsRemaining": 0}
        )
    return {
        "success": True,
        "data": rows,
        "environmentProvider": health.get(
            _providers.ENV_PROVIDER_ID,
            {"consecutiveFailures": 0, "benched": False, "secondsRemaining": 0},
        ),
    }


@router.get("/proxy-spend")
async def proxy_spend(_: InternalDep, days: int = 30) -> dict[str, Any]:
    """Measured proxy bytes and their estimated cost, by provider and by type.

    Priced with the same per-GB figures the router orders by, so the desk sees
    what "cheapest first" is actually saving. Every proxied ATTEMPT is in the
    ledger, including the exits the deep rungs choose for themselves.
    """
    from engine.core.proxy import budget

    return {"success": True, "data": await budget.spend_by_provider(days=max(1, min(days, 90)))}


@router.post("/proxy-providers", status_code=201)
async def create_proxy_provider(body: CreateProvider, _: InternalDep) -> dict[str, Any]:
    row = await repo.create_proxy_provider(body.model_dump())
    _providers_changed()
    return {"success": True, "data": row}


@router.patch("/proxy-providers/{provider_id}")
async def update_proxy_provider(
    provider_id: str, body: UpdateProvider, _: InternalDep
) -> dict[str, Any]:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise InvalidRequest("Nothing to update")
    if not await repo.update_proxy_provider(provider_id, fields):
        raise JobNotFound(provider_id)
    _providers_changed()
    return {"success": True, "data": await repo.get_proxy_provider(provider_id)}


@router.delete("/proxy-providers/{provider_id}")
async def delete_proxy_provider(provider_id: str, _: InternalDep) -> dict[str, Any]:
    if not await repo.delete_proxy_provider(provider_id):
        raise JobNotFound(provider_id)
    _providers_changed()
    return {"success": True}
