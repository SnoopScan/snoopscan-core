"""Repository layer — every durable read and write in one place.

Notable behaviours mandated by the specs:

  * The cache IS the `pages` table (07-orchestration.md s5). There is no
    separate cache store, so there is no invalidation problem and no divergence
    between cache and record.
  * Frontier claiming is one atomic statement with FOR UPDATE SKIP LOCKED
    (02-data-model.md s3) — no advisory locks, no select-then-update race.
  * Page writes upsert on (job_id, normalized_hash) so a reaped-and-rerun claim
    cannot duplicate a row (07-orchestration.md s6).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from engine.core.fetch.escalation import DomainProfile
from engine.core.models import Tier
from engine.storage import db
from engine.storage.ids import new_id

_WHITESPACE = re.compile(r"\s+")


def content_hash(text: str) -> bytes:
    """SHA-256 of normalised extracted text.

    Normalisation before hashing matters: without it a timestamp or a view
    counter in the page makes every fetch look changed.
    """
    normalised = _WHITESPACE.sub(" ", text.lower()).strip()
    return hashlib.sha256(normalised.encode("utf-8")).digest()


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------


@dataclass
class ApiKey:
    id: str
    label: str
    scopes: list[str]
    rate_limit_rpm: int
    allow_js_exec: bool
    webhook_secret: str | None
    active: bool
    owner_ref: str | None = None
    credits_remaining: int = 0  # the OWNER's balance, shared across their keys
    concurrency: int = 5
    suspended: bool = False


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


async def get_api_key(plaintext: str) -> ApiKey | None:
    row = await db.fetchrow(
        """
        SELECT k.id, k.label, k.scopes, k.rate_limit_rpm, k.allow_js_exec, k.webhook_secret,
               k.active, k.owner_ref,
               COALESCE(o.credits_remaining, 0) AS credits_remaining,
               COALESCE(o.concurrency, 5) AS concurrency,
               COALESCE(o.suspended, false) AS suspended
        FROM api_keys k LEFT JOIN owners o ON o.owner_ref = k.owner_ref
        WHERE k.key_hash = $1 AND k.active
        """,
        hash_key(plaintext),
    )
    if row is None:
        return None
    return ApiKey(
        id=row["id"],
        label=row["label"],
        scopes=list(row["scopes"]),
        rate_limit_rpm=row["rate_limit_rpm"],
        allow_js_exec=row["allow_js_exec"],
        webhook_secret=row["webhook_secret"],
        active=row["active"],
        owner_ref=row["owner_ref"],
        credits_remaining=row["credits_remaining"],
        concurrency=row["concurrency"],
        suspended=row["suspended"],
    )


async def touch_api_key(key_id: str) -> None:
    await db.execute("UPDATE api_keys SET last_used_at = now() WHERE id = $1", key_id)


async def create_api_key(
    plaintext: str,
    label: str,
    *,
    owner_ref: str,
    scopes: list[str] | None = None,
    rate_limit_rpm: int = 60,
    allow_js_exec: bool = False,
    webhook_secret: str | None = None,
) -> str:
    """The plaintext key is shown once at creation and never stored.

    `owner_ref` is required. It used to default to None, and a key with no
    owner was silently exempt from metering for its whole life — three such
    keys ran on production for days and recorded not one row of usage. An
    operator key belongs to `credits.OPERATOR_OWNER`, not to nobody.
    """
    key_id = new_id("key")
    await ensure_owner(owner_ref)
    await db.execute(
        """
        INSERT INTO api_keys (id, key_hash, label, scopes, rate_limit_rpm,
                              allow_js_exec, webhook_secret, owner_ref, prefix)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        key_id,
        hash_key(plaintext),
        label,
        scopes or ["scrape", "crawl", "map"],
        rate_limit_rpm,
        allow_js_exec,
        webhook_secret,
        owner_ref,
        plaintext[:11],
    )
    return key_id


async def ensure_owner(owner_ref: str) -> None:
    await db.execute(
        "INSERT INTO owners (owner_ref) VALUES ($1) ON CONFLICT (owner_ref) DO NOTHING",
        owner_ref,
    )


async def get_owner(owner_ref: str) -> dict[str, Any] | None:
    row = await db.fetchrow(
        """
        SELECT owner_ref, credits_remaining, concurrency, suspended, updated_at
        FROM owners WHERE owner_ref = $1
        """,
        owner_ref,
    )
    if row is None:
        return None
    return {
        "owner_ref": row["owner_ref"],
        "credits_remaining": int(row["credits_remaining"]),
        "concurrency": int(row["concurrency"]),
        "suspended": bool(row["suspended"]),
        "updated_at": row["updated_at"].isoformat(),
    }


async def update_owner(owner_ref: str, **fields: Any) -> bool:
    allowed = {"concurrency", "suspended"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update {sorted(bad)}")
    if not fields:
        return False
    await ensure_owner(owner_ref)
    # Column names come from the closed allowlist above, never from the caller.
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    query = f"UPDATE owners SET {sets}, updated_at = now() WHERE owner_ref = $1"  # noqa: S608
    status = await db.execute(query, owner_ref, *fields.values())
    return status.endswith("1")


# ---------------------------------------------------------------------------
# Billing — the app owns customers and grants; the engine enforces and meters.
# ---------------------------------------------------------------------------


def _key_from_row(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "label": row["label"],
        "prefix": row["prefix"],
        "scopes": list(row["scopes"]),
        "rate_limit_rpm": row["rate_limit_rpm"],
        "allow_js_exec": row["allow_js_exec"],
        "active": row["active"],
        "owner_ref": row["owner_ref"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
    }


async def list_api_keys(owner_ref: str | None = None) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT id, label, prefix, scopes, rate_limit_rpm, allow_js_exec, active,
               owner_ref, created_at, last_used_at
        FROM api_keys
        WHERE ($1::text IS NULL OR owner_ref = $1)
        ORDER BY created_at DESC
        """,
        owner_ref,
    )
    return [_key_from_row(r) for r in rows]


async def update_api_key(key_id: str, **fields: Any) -> bool:
    allowed = {"label", "active", "rate_limit_rpm", "allow_js_exec", "scopes"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update {sorted(bad)}")
    if not fields:
        return False
    # Column names come from the closed allowlist above, never from the caller.
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    query = f"UPDATE api_keys SET {sets} WHERE id = $1"  # noqa: S608
    status = await db.execute(query, key_id, *fields.values())
    return status.endswith("1")


async def adjust_credits(owner_ref: str, delta: int) -> int:
    """Grant (or claw back) an owner's credits atomically. Returns the new balance."""
    await ensure_owner(owner_ref)
    val = await db.fetchval(
        """
        UPDATE owners
        SET credits_remaining = GREATEST(0, credits_remaining + $2), updated_at = now()
        WHERE owner_ref = $1 RETURNING credits_remaining
        """,
        owner_ref,
        delta,
    )
    return int(val or 0)


async def get_credit_costs() -> dict[str, int]:
    rows = await db.fetch("SELECT key, credits FROM credit_costs")
    return {r["key"]: int(r["credits"]) for r in rows}


async def set_credit_costs(table: dict[str, int]) -> None:
    async with db.transaction() as conn:
        for key, credits in table.items():
            await conn.execute(
                """
                INSERT INTO credit_costs (key, credits, updated_at) VALUES ($1, $2, now())
                ON CONFLICT (key) DO UPDATE SET credits = EXCLUDED.credits, updated_at = now()
                """,
                key,
                int(credits),
            )


async def record_usage(
    key_id: str,
    *,
    owner_ref: str,
    endpoint: str,
    host: str | None,
    tier: str | None,
    proxy_bytes: int,
    cached: bool,
    pdf_pages: int,
    credits: int,
    cache_own: bool = False,
    job_id: str | None = None,
    url: str | None = None,
) -> int:
    """Meter one successful response: event, daily rollup, balance — one transaction.

    Returns the balance after charging. The balance floors at zero; a request
    that was allowed through at 1 credit and cost 5 is charged what is there.
    """
    # From the pricer, not derived again here. This was a fifth copy of the
    # tier-to-key rule and it did not know about `cached_shared`, so every
    # foreign cache hit rolled up as a free one.
    from engine.core.credits import key_for
    from engine.core.models import Cost

    kind = key_for(Cost(tier=tier, proxy_bytes=proxy_bytes, cached=cached, cache_own=cache_own))
    async with db.transaction() as conn:
        await conn.execute(
            """
            INSERT INTO usage_events (api_key_id, job_id, endpoint, host, url, tier, proxy_bytes,
                                      cached, pdf_pages, credits)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            key_id,
            job_id,
            endpoint,
            host,
            url[:2048] if url else None,
            tier,
            proxy_bytes,
            cached,
            pdf_pages,
            credits,
        )
        await conn.execute(
            """
            INSERT INTO usage_daily (api_key_id, day, requests, credits,
                                     direct, proxied, browser, cached)
            VALUES ($1, CURRENT_DATE, 1, $2, $3, $4, $5, $6)
            ON CONFLICT (api_key_id, day) DO UPDATE SET
                requests = usage_daily.requests + 1,
                credits  = usage_daily.credits + EXCLUDED.credits,
                direct   = usage_daily.direct + EXCLUDED.direct,
                proxied  = usage_daily.proxied + EXCLUDED.proxied,
                browser  = usage_daily.browser + EXCLUDED.browser,
                cached   = usage_daily.cached + EXCLUDED.cached
            """,
            key_id,
            credits,
            int(kind == "direct"),
            int(kind == "proxied"),
            int(kind == "browser"),
            int(kind == "cached"),
        )
        # One row per cost key, so a key added on the desk rolls up with no
        # migration. The four columns above are a subset kept for the chart.
        await conn.execute(
            """
            INSERT INTO usage_daily_costs (api_key_id, day, cost_key, requests, credits)
            VALUES ($1, CURRENT_DATE, $2, 1, $3)
            ON CONFLICT (api_key_id, day, cost_key) DO UPDATE SET
                requests = usage_daily_costs.requests + 1,
                credits  = usage_daily_costs.credits + EXCLUDED.credits
            """,
            key_id,
            kind,
            credits,
        )
        val = await conn.fetchval(
            """
            UPDATE owners SET credits_remaining = GREATEST(0, credits_remaining - $2),
                              updated_at = now()
            WHERE owner_ref = $1 RETURNING credits_remaining
            """,
            owner_ref,
            credits,
        )
    return int(val or 0)


async def job_api_key(job_id: str) -> ApiKey | None:
    row = await db.fetchrow(
        """
        SELECT k.id, k.label, k.scopes, k.rate_limit_rpm, k.allow_js_exec, k.webhook_secret,
               k.active, k.owner_ref,
               COALESCE(o.credits_remaining, 0) AS credits_remaining,
               COALESCE(o.concurrency, 5) AS concurrency,
               COALESCE(o.suspended, false) AS suspended
        FROM jobs j JOIN api_keys k ON k.id = j.api_key_id
        LEFT JOIN owners o ON o.owner_ref = k.owner_ref
        WHERE j.id = $1
        """,
        job_id,
    )
    if row is None:
        return None
    return ApiKey(
        id=row["id"],
        label=row["label"],
        scopes=list(row["scopes"]),
        rate_limit_rpm=row["rate_limit_rpm"],
        allow_js_exec=row["allow_js_exec"],
        webhook_secret=row["webhook_secret"],
        active=row["active"],
        owner_ref=row["owner_ref"],
        credits_remaining=row["credits_remaining"],
        concurrency=row["concurrency"],
        suspended=row["suspended"],
    )


# ---------------------------------------------------------------------------
# Proxy providers — the vendor registry the desk edits.
# ---------------------------------------------------------------------------

PROVIDER_COLUMNS = (
    "id, name, type::text AS type, host, port, username, country, username_template, "
    "password_template, password_sticky_template, sticky_lifetime_minutes, enabled, priority, "
    "grade, cost_per_gb::float8 AS cost_per_gb, created_at, updated_at"
)


def _provider_row(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    for key in ("created_at", "updated_at"):
        if d.get(key) is not None:
            d[key] = d[key].isoformat()
    return d


async def list_proxy_providers(enabled_only: bool = False) -> list[dict[str, Any]]:
    rows = await db.fetch(
        f"""
        SELECT {PROVIDER_COLUMNS} FROM proxy_providers
        WHERE ($1::bool IS FALSE OR enabled)
        ORDER BY priority, created_at
        """,  # noqa: S608
        enabled_only,
    )
    return [_provider_row(r) for r in rows]


async def get_proxy_provider(provider_id: str) -> dict[str, Any] | None:
    row = await db.fetchrow(
        f"SELECT {PROVIDER_COLUMNS} FROM proxy_providers WHERE id = $1",  # noqa: S608
        provider_id,
    )
    return _provider_row(row) if row else None


async def get_proxy_provider_secret(provider_id: str) -> str | None:
    """The decrypted password — for building an endpoint, never for a response."""
    from engine.core.secrets import decrypt

    enc = await db.fetchval("SELECT password_enc FROM proxy_providers WHERE id = $1", provider_id)
    return decrypt(enc) if enc else None


def _money(value: Any) -> Decimal | None:
    """A price for a numeric column: via str, so 3.75 is stored as 3.75."""
    return None if value is None else Decimal(str(value))


async def create_proxy_provider(fields: dict[str, Any]) -> dict[str, Any]:
    from engine.core.secrets import encrypt

    provider_id = new_id("prov")
    await db.execute(
        """
        INSERT INTO proxy_providers (id, name, type, host, port, username, password_enc, country,
                                     username_template, password_template,
                                     password_sticky_template, sticky_lifetime_minutes,
                                     enabled, priority, grade, cost_per_gb)
        VALUES ($1, $2, $3::proxy_type, $4, $5, $6, $7, $8,
                COALESCE($9, '{username}'),
                COALESCE($10, '{password}_country-{country}'),
                COALESCE(
                    $11,
                    '{password}_country-{country}_session-{session}_lifetime-{lifetime}m'
                ),
                COALESCE($12, 10), COALESCE($13, true), COALESCE($14, 100),
                COALESCE($15, 'premium'), $16)
        """,
        provider_id,
        fields["name"],
        fields["type"],
        fields["host"],
        int(fields["port"]),
        fields["username"],
        encrypt(fields["password"]),
        fields.get("country"),
        fields.get("username_template"),
        fields.get("password_template"),
        fields.get("password_sticky_template"),
        fields.get("sticky_lifetime_minutes"),
        fields.get("enabled"),
        fields.get("priority"),
        fields.get("grade"),
        _money(fields.get("cost_per_gb")),
    )
    row = await get_proxy_provider(provider_id)
    assert row is not None
    return row


async def update_proxy_provider(provider_id: str, fields: dict[str, Any]) -> bool:
    from engine.core.secrets import encrypt

    allowed = {
        "name",
        "type",
        "host",
        "port",
        "username",
        "country",
        "username_template",
        "password_template",
        "password_sticky_template",
        "sticky_lifetime_minutes",
        "enabled",
        "priority",
        "grade",
        "cost_per_gb",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "cost_per_gb" in updates:
        updates["cost_per_gb"] = _money(updates["cost_per_gb"])
    if fields.get("password"):
        updates["password_enc"] = encrypt(fields["password"])
    if not updates:
        return False
    # Column names come from the closed allowlist above, never from the caller.
    sets = ", ".join(
        f"{k} = ${i + 2}" + ("::proxy_type" if k == "type" else "") for i, k in enumerate(updates)
    )
    query = f"UPDATE proxy_providers SET {sets}, updated_at = now() WHERE id = $1"  # noqa: S608
    status = await db.execute(query, provider_id, *updates.values())
    return status.endswith("1")


# How long a provider's refusal of a domain stands before it is tried again. A
# policy (a provider's ID-verification list) changes on the order of months.
PROVIDER_REFUSAL_DAYS = 30


async def record_provider_refusal(
    provider_id: str, domain: str, days: int = PROVIDER_REFUSAL_DAYS
) -> None:
    """Note that a provider refused to carry this domain."""
    await db.execute(
        """
        INSERT INTO proxy_provider_refusals (provider_id, domain, expires_at)
        VALUES ($1, $2, now() + ($3 || ' days')::interval)
        ON CONFLICT (provider_id, domain) DO UPDATE SET
            refused_at = now(),
            expires_at = EXCLUDED.expires_at,
            refusals   = proxy_provider_refusals.refusals + 1
        """,
        provider_id,
        domain,
        str(days),
    )


async def provider_domain_record(days: int = 7) -> list[dict[str, Any]]:
    """Per provider and domain: how many proxied attempts got through, and how
    many were blocked, over the last `days`. Read by the provider picker so a
    provider that keeps failing a site where another succeeds stops being sent
    there. The provider id is the second part of a registry exit's id
    (`res-<provider>-<country>-<session>`)."""
    rows = await db.fetch(
        """
        SELECT split_part(proxy_id, '-', 2) AS provider_id, domain,
               count(*) FILTER (WHERE outcome = 'success') AS successes,
               count(*) FILTER (WHERE outcome = 'blocked') AS blocks
        FROM fetch_log
        WHERE proxy_id LIKE '%-prov\\_%' ESCAPE '\\'
          AND recorded_at > now() - ($1 || ' days')::interval
        GROUP BY 1, 2
        """,
        str(days),
    )
    return [dict(r) for r in rows]


async def list_provider_refusals() -> list[dict[str, Any]]:
    """Every refusal still standing."""
    rows = await db.fetch(
        "SELECT provider_id, domain, refused_at, refusals FROM proxy_provider_refusals "
        "WHERE expires_at > now() ORDER BY provider_id, domain"
    )
    return [dict(r) for r in rows]


async def delete_proxy_provider(provider_id: str) -> bool:
    status = await db.execute("DELETE FROM proxy_providers WHERE id = $1", provider_id)
    return status.endswith("1")


BROWSER_CLASS = frozenset({"browser", "stealth", "stealth_hard", "mobile"})


async def usage_summary(
    *, owner_ref: str | None = None, key_id: str | None = None, days: int = 30
) -> dict[str, Any]:
    """Daily rollups for one owner or one key, plus totals. Reads usage_daily only."""
    rows = await db.fetch(
        """
        SELECT d.day, SUM(d.requests) AS requests, SUM(d.credits) AS credits,
               SUM(d.direct) AS direct, SUM(d.proxied) AS proxied,
               SUM(d.browser) AS browser, SUM(d.cached) AS cached
        FROM usage_daily d JOIN api_keys k ON k.id = d.api_key_id
        WHERE d.day >= CURRENT_DATE - ($3::int - 1)
          AND ($1::text IS NULL OR k.owner_ref = $1)
          AND ($2::text IS NULL OR d.api_key_id = $2)
        GROUP BY d.day ORDER BY d.day
        """,
        owner_ref,
        key_id,
        days,
    )
    daily = [
        {
            "day": r["day"].isoformat(),
            "requests": int(r["requests"]),
            "credits": int(r["credits"]),
            "direct": int(r["direct"]),
            "proxied": int(r["proxied"]),
            "browser": int(r["browser"]),
            "cached": int(r["cached"]),
        }
        for r in rows
    ]
    # Every cost key that billed, so the app's breakdown always sums to the
    # total beside it. Four hardcoded tier columns could never do that.
    by_cost = await db.fetch(
        """
        SELECT c.cost_key, SUM(c.requests) AS requests, SUM(c.credits) AS credits
        FROM usage_daily_costs c JOIN api_keys k ON k.id = c.api_key_id
        WHERE c.day >= CURRENT_DATE - ($3::int - 1)
          AND ($1::text IS NULL OR k.owner_ref = $1)
          AND ($2::text IS NULL OR c.api_key_id = $2)
        GROUP BY c.cost_key ORDER BY SUM(c.credits) DESC
        """,
        owner_ref,
        key_id,
        days,
    )

    by_endpoint = await db.fetch(
        """
        SELECT e.endpoint, COUNT(*) AS requests, COALESCE(SUM(e.credits), 0) AS credits
        FROM usage_events e JOIN api_keys k ON k.id = e.api_key_id
        WHERE e.recorded_at >= CURRENT_DATE - ($3::int - 1)
          AND ($1::text IS NULL OR k.owner_ref = $1)
          AND ($2::text IS NULL OR e.api_key_id = $2)
        GROUP BY e.endpoint ORDER BY credits DESC
        """,
        owner_ref,
        key_id,
        days,
    )
    return {
        "days": days,
        "daily": daily,
        "requests": sum(d["requests"] for d in daily),
        "credits": sum(d["credits"] for d in daily),
        "by_endpoint": [
            {
                "endpoint": r["endpoint"],
                "requests": int(r["requests"]),
                "credits": int(r["credits"]),
            }
            for r in by_endpoint
        ],
        "by_cost": [
            {
                "key": r["cost_key"],
                "requests": int(r["requests"]),
                "credits": int(r["credits"]),
            }
            for r in by_cost
        ],
    }


async def list_usage_events(
    *, owner_ref: str | None = None, key_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """The customer's request log: every METERED response, newest first. Failed
    requests are never charged, so they never appear here — the status column a
    desk shows is always a success."""
    rows = await db.fetch(
        """
        SELECT e.id, e.api_key_id, k.label AS key_label, k.prefix AS key_prefix, e.job_id,
               e.endpoint, e.host, e.url, e.tier, e.proxy_bytes, e.cached, e.pdf_pages, e.credits,
               e.recorded_at
        FROM usage_events e JOIN api_keys k ON k.id = e.api_key_id
        WHERE ($1::text IS NULL OR k.owner_ref = $1)
          AND ($2::text IS NULL OR e.api_key_id = $2)
        ORDER BY e.id DESC LIMIT $3
        """,
        owner_ref,
        key_id,
        limit,
    )
    return [
        {
            "id": int(r["id"]),
            "api_key_id": r["api_key_id"],
            "key_label": r["key_label"],
            "key_prefix": r["key_prefix"],
            "job_id": r["job_id"],
            "endpoint": r["endpoint"],
            "host": r["host"],
            "url": r["url"],
            "tier": r["tier"],
            "proxy_bytes": int(r["proxy_bytes"]),
            "cached": bool(r["cached"]),
            "pdf_pages": int(r["pdf_pages"]),
            "credits": int(r["credits"]),
            "recorded_at": r["recorded_at"].isoformat(),
        }
        for r in rows
    ]


async def health_summary() -> dict[str, Any]:
    """What the Engine › Health page shows. Every figure comes from a table
    that exists; the 24h window keeps the queries cheap on fetch_log."""
    from engine.settings import get_settings

    jobs = await db.fetch(
        """
        SELECT status::text AS status, COUNT(*) AS n FROM jobs
        WHERE created_at >= now() - interval '24 hours' GROUP BY status
        """
    )
    fetch = await db.fetchrow(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE outcome = 'success') AS ok,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50
        FROM fetch_log WHERE recorded_at >= now() - interval '24 hours'
        """
    )
    tiers = await db.fetch(
        """
        SELECT tier, COUNT(*) AS n FROM fetch_log
        WHERE recorded_at >= now() - interval '24 hours' AND outcome = 'success'
        GROUP BY tier
        """
    )
    queue = await db.fetchrow(
        """
        SELECT
          (SELECT COUNT(*) FROM jobs WHERE status = 'queued')  AS jobs_queued,
          (SELECT COUNT(*) FROM jobs WHERE status = 'running') AS jobs_running,
          (SELECT COUNT(*) FROM frontier WHERE status = 'pending') AS frontier_pending
        """
    )
    proxy = await db.fetchrow(
        """
        -- The rollup never holds TODAY (it aggregates finished days), so this
        -- read the day's spend as 0 MB, every day. The source table does.
        SELECT COALESCE(SUM(bytes), 0) AS bytes, COUNT(*) AS requests
        FROM proxy_usage WHERE recorded_at >= date_trunc('day', now())
        """
    )
    domains = await db.fetchval("SELECT COUNT(*) FROM domain_profiles")
    circuits = await db.fetchval(
        """
        SELECT COUNT(*) FROM domain_profiles
        WHERE circuit_open_until IS NOT NULL AND circuit_open_until > now()
        """
    )
    keys = await db.fetchval("SELECT COUNT(*) FROM api_keys WHERE active")
    proxies = await db.fetchval("SELECT COUNT(*) FROM proxies WHERE active")
    total = int(fetch["total"] or 0) if fetch else 0
    ok = int(fetch["ok"] or 0) if fetch else 0
    return {
        "database": await db.healthy(),
        "jobs_24h": {r["status"]: int(r["n"]) for r in jobs},
        "fetches_24h": total,
        "success_rate_24h": (ok / total) if total else None,
        "p50_latency_ms": int(fetch["p50"]) if fetch and fetch["p50"] is not None else None,
        "tier_mix_24h": {r["tier"]: int(r["n"]) for r in tiers},
        "queue_depth": {
            "jobs_queued": int(queue["jobs_queued"] or 0) if queue else 0,
            "jobs_running": int(queue["jobs_running"] or 0) if queue else 0,
            "frontier_pending": int(queue["frontier_pending"] or 0) if queue else 0,
        },
        "proxy_today": {
            "bytes": int(proxy["bytes"] or 0) if proxy else 0,
            "requests": int(proxy["requests"] or 0) if proxy else 0,
            "budget_mb": get_settings().proxy_daily_budget_mb,
        },
        "domains": int(domains or 0),
        "circuits_open": int(circuits or 0),
        "active_keys": int(keys or 0),
        "proxies": int(proxies or 0),
    }


async def list_jobs(limit: int = 50, owner_ref: str | None = None) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT j.id, j.kind, j.status, j.api_key_id, j.total, j.completed, j.failed,
               j.cost, j.created_at, j.completed_at, k.owner_ref
        FROM jobs j JOIN api_keys k ON k.id = j.api_key_id
        WHERE ($2::text IS NULL OR k.owner_ref = $2)
        ORDER BY j.created_at DESC LIMIT $1
        """,
        limit,
        owner_ref,
    )
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "status": r["status"],
            "api_key_id": r["api_key_id"],
            "owner_ref": r["owner_ref"],
            "total": r["total"],
            "completed": r["completed"],
            "failed": r["failed"],
            "cost": r["cost"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
        }
        for r in rows
    ]


async def list_domain_profiles(
    limit: int = 100, sort: str = "recent", q: str | None = None
) -> list[dict[str, Any]]:
    """What the engine has learned per domain. Columns as in 0001 + 0003."""
    order = {
        "recent": "last_success_at DESC NULLS LAST",
        "hardest": "min_working_tier DESC NULLS LAST, last_success_at DESC NULLS LAST",
        "blocked": "circuit_open_until DESC NULLS LAST, block_count DESC",
        "busiest": "success_count + failure_count DESC",
    }.get(sort, "last_success_at DESC NULLS LAST")
    # `order` is one of four literals from the dict above, never caller input.
    rows = await db.fetch(
        f"""
        SELECT domain, min_working_tier, requires_proxy, required_proxy_type, detected_waf,
               working_country, success_count, failure_count, block_count,
               circuit_open_until, last_success_at, last_block_at
        FROM domain_profiles
        WHERE ($2::text IS NULL OR domain ILIKE '%' || $2 || '%')
        ORDER BY {order} LIMIT $1
        """,  # noqa: S608
        limit,
        q,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for key in ("circuit_open_until", "last_success_at", "last_block_at"):
            if d.get(key) is not None:
                d[key] = d[key].isoformat()
        out.append(d)
    return out


async def list_proxies() -> list[dict[str, Any]]:
    """The proxy fleet with today's usage. Credentials never leave the table."""
    rows = await db.fetch(
        """
        SELECT p.id, p.type::text AS type, p.endpoint, p.country, p.sticky_capable, p.active,
               p.retired_at, p.retired_reason, p.created_at,
               COALESCE(u.bytes, 0) AS bytes_today, COALESCE(u.requests, 0) AS requests_today,
               COALESCE(u.successes, 0) AS successes_today
        FROM proxies p
        LEFT JOIN (
            SELECT proxy_id, SUM(bytes) AS bytes, SUM(requests) AS requests,
                   SUM(successes) AS successes
            FROM proxy_usage_daily WHERE day = CURRENT_DATE GROUP BY proxy_id
        ) u ON u.proxy_id = p.id
        ORDER BY p.active DESC, p.created_at
        """
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for key in ("retired_at", "created_at"):
            if d.get(key) is not None:
                d[key] = d[key].isoformat()
        for key in ("bytes_today", "requests_today", "successes_today"):
            d[key] = int(d[key] or 0)
        out.append(d)
    return out


# --------------------------------------------------------------------------
# Extraction templates (0035). The desk's additions and overrides.
# --------------------------------------------------------------------------


async def list_extraction_templates() -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT name, description, schema, active FROM extraction_templates ORDER BY name"
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if isinstance(item.get("schema"), str):
            # asyncpg hands jsonb back as text unless a codec is registered.
            item["schema"] = json.loads(item["schema"])
        out.append(item)
    return out


async def upsert_extraction_template(
    name: str, schema: dict[str, Any], description: str = "", active: bool = True
) -> dict[str, Any]:
    await db.execute(
        """
        INSERT INTO extraction_templates (name, description, schema, active)
        VALUES ($1, $2, $3::jsonb, $4)
        ON CONFLICT (name) DO UPDATE SET
            description = EXCLUDED.description,
            schema = EXCLUDED.schema,
            active = EXCLUDED.active,
            updated_at = now()
        """,
        name,
        description,
        json.dumps(schema),
        active,
    )
    rows = [t for t in await list_extraction_templates() if t["name"] == name]
    return rows[0] if rows else {}


async def delete_extraction_template(name: str) -> bool:
    status = await db.execute("DELETE FROM extraction_templates WHERE name = $1", name)
    return status.endswith("1")


# --------------------------------------------------------------------------
# Fleet benchmarks (0034). What the engine could read, and when.
# --------------------------------------------------------------------------


async def start_benchmark_run(label: str, engine_sha: str | None = None) -> str:
    run_id = new_id("bench")
    await db.execute(
        "INSERT INTO benchmark_runs (id, label, engine_sha) VALUES ($1, $2, $3)",
        run_id,
        label,
        engine_sha,
    )
    return run_id


async def record_benchmark_result(run_id: str, row: dict[str, Any]) -> None:
    """One site's outcome. `self_inflicted` is the column worth reading."""
    await db.execute(
        """
        INSERT INTO benchmark_results (
            run_id, rank, domain, url, ok, error_code, signal, tier, tiers_attempted,
            words, credits, proxy_bytes, elapsed_ms, control_ok, control_words, self_inflicted
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
        """,
        run_id,
        row.get("rank"),
        row["domain"],
        row["url"],
        bool(row["ok"]),
        row.get("error_code"),
        row.get("signal"),
        row.get("tier"),
        ",".join(row.get("tiers_attempted") or []) or None,
        row.get("words"),
        row.get("credits"),
        row.get("proxy_bytes"),
        row.get("elapsed_ms"),
        row.get("control_ok"),
        row.get("control_words"),
        bool(row.get("self_inflicted")),
    )


async def finish_benchmark_run(run_id: str, notes: str | None = None) -> dict[str, Any]:
    await db.execute(
        """
        UPDATE benchmark_runs SET
            finished_at = now(),
            total  = (SELECT count(*) FROM benchmark_results WHERE run_id = $1),
            passed = (SELECT count(*) FROM benchmark_results WHERE run_id = $1 AND ok),
            notes  = COALESCE($2, notes)
        WHERE id = $1
        """,
        run_id,
        notes,
    )
    row = await db.fetchrow("SELECT * FROM benchmark_runs WHERE id = $1", run_id)
    return dict(row) if row else {}


async def benchmark_runs(limit: int = 20) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT r.*,
               (SELECT count(*) FROM benchmark_results x
                 WHERE x.run_id = r.id AND x.self_inflicted) AS self_inflicted
        FROM benchmark_runs r ORDER BY r.started_at DESC LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def benchmark_failures(run_id: str, ours_only: bool = False) -> list[dict[str, Any]]:
    rows = await db.fetch(
        f"""
        SELECT rank, domain, error_code, signal, tiers_attempted, control_ok,
               control_words, self_inflicted, elapsed_ms
        FROM benchmark_results
        WHERE run_id = $1 AND NOT ok {"AND self_inflicted" if ours_only else ""}
        ORDER BY self_inflicted DESC, rank NULLS LAST
        """,  # noqa: S608 - the only interpolation is a fixed clause, not input
        run_id,
    )
    return [dict(r) for r in rows]


async def reset_domain_circuit(domain: str) -> bool:
    """Close one domain's circuit breaker and reset its backoff. Nothing else.

    The backoff doubles on each opening, which is right for a site that is
    down and wrong once the cause was ours and has been fixed. What was
    learned about the domain — its WAF, tier floor, working country, counts —
    is kept; only the lockout goes. False if there is no such profile.
    """
    status = await db.execute(
        """
        UPDATE domain_profiles
        SET circuit_open_until = NULL, circuit_opens = 0
        WHERE domain = $1
        """,
        domain,
    )
    return status.endswith("1")


async def load_domain_profile(domain: str) -> DomainProfile:
    row = await db.fetchrow(
        """
        SELECT domain, min_working_tier, requires_proxy, required_proxy_type, detected_waf,
               success_count, failure_count, block_count,
               avg_content_length, stdev_content_length, circuit_open_until,
               working_country, country_attempts, climbs_above_floor, blocks_at_floor,
               circuit_opens, avg_success_ms, stdev_success_ms, timed_success_count
        FROM domain_profiles WHERE domain = $1
        """,
        domain,
    )
    if row is None:
        return DomainProfile(domain=domain)
    circuit = row["circuit_open_until"]
    return DomainProfile(
        domain=row["domain"],
        min_working_tier=Tier(row["min_working_tier"]),
        requires_proxy=row["requires_proxy"],
        required_proxy_type=row["required_proxy_type"],
        detected_waf=row["detected_waf"],
        success_count=row["success_count"],
        failure_count=row["failure_count"],
        block_count=row["block_count"],
        avg_content_length=row["avg_content_length"],
        stdev_content_length=row["stdev_content_length"],
        circuit_open_until=circuit.timestamp() if circuit else None,
        working_country=row["working_country"],
        country_attempts=tuple(row["country_attempts"] or ()),
        climbs_above_floor=row["climbs_above_floor"] or 0,
        blocks_at_floor=row["blocks_at_floor"] or 0,
        circuit_opens=row["circuit_opens"] or 0,
        avg_success_ms=row["avg_success_ms"],
        stdev_success_ms=row["stdev_success_ms"],
        timed_success_count=row["timed_success_count"] or 0,
    )


# Countries tried, in order, when a domain blocks us and we do not yet know
# which one it answers. US first because it is where the one measured geo-gate
# opened (etsy.com: 0/10 from GB, 10/10 from US) and because most of the web is
# served US-first. Deliberately SHORT: every extra country is another failed
# request paid for in bandwidth and latency, and a domain that blocks all of
# them is blocking us for a reason a passport will not fix.
COUNTRY_FALLBACKS: tuple[str, ...] = ("us", "gb", "de")


async def record_working_country(domain: str, country: str) -> None:
    """Remember that this domain answered from `country`.

    Learn-once, same shape as `min_working_tier`. The next request for this
    domain starts from the country that worked instead of rediscovering it.
    """
    await db.execute(
        """
        INSERT INTO domain_profiles (domain, working_country)
        VALUES ($1, $2)
        ON CONFLICT (domain) DO UPDATE SET working_country = EXCLUDED.working_country
        """,
        domain,
        country,
    )


async def note_country_attempt(domain: str, country: str) -> None:
    """Record a country that did NOT work, so it is not retried for ever.

    Without this the fallback list runs on every request to a domain that
    answers from nowhere, turning one geo-gate into a permanent 3x cost
    multiplier on that domain.
    """
    await db.execute(
        """
        INSERT INTO domain_profiles (domain, country_attempts)
        VALUES ($1, ARRAY[$2::text])
        ON CONFLICT (domain) DO UPDATE
        SET country_attempts =
            CASE WHEN domain_profiles.country_attempts @> ARRAY[$2::text]
                 THEN domain_profiles.country_attempts
                 ELSE domain_profiles.country_attempts || ARRAY[$2::text] END
        """,
        domain,
        country,
    )


async def save_domain_profile(
    profile: DomainProfile, *, last_success: bool = False, last_block: bool = False
) -> None:
    await db.execute(
        """
        INSERT INTO domain_profiles (
            domain, min_working_tier, requires_proxy, required_proxy_type, detected_waf,
            success_count, failure_count, block_count,
            avg_content_length, stdev_content_length,
            last_success_at, last_block_at, circuit_open_until, climbs_above_floor,
            blocks_at_floor, circuit_opens, avg_success_ms, stdev_success_ms, timed_success_count
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                  CASE WHEN $11 THEN now() END,
                  CASE WHEN $12 THEN now() END,
                  $13,$14,$15,$16,$17,$18,$19)
        ON CONFLICT (domain) DO UPDATE SET
            min_working_tier     = EXCLUDED.min_working_tier,
            requires_proxy       = EXCLUDED.requires_proxy,
            required_proxy_type  = EXCLUDED.required_proxy_type,
            detected_waf         = COALESCE(EXCLUDED.detected_waf, domain_profiles.detected_waf),
            success_count        = EXCLUDED.success_count,
            failure_count        = EXCLUDED.failure_count,
            block_count          = EXCLUDED.block_count,
            avg_content_length   = EXCLUDED.avg_content_length,
            stdev_content_length = EXCLUDED.stdev_content_length,
            last_success_at      = COALESCE(EXCLUDED.last_success_at,
                                            domain_profiles.last_success_at),
            last_block_at        = COALESCE(EXCLUDED.last_block_at,
                                            domain_profiles.last_block_at),
            circuit_open_until   = EXCLUDED.circuit_open_until,
            climbs_above_floor   = EXCLUDED.climbs_above_floor,
            blocks_at_floor      = EXCLUDED.blocks_at_floor,
            circuit_opens        = EXCLUDED.circuit_opens,
            avg_success_ms       = EXCLUDED.avg_success_ms,
            stdev_success_ms     = EXCLUDED.stdev_success_ms,
            timed_success_count  = EXCLUDED.timed_success_count
        """,
        profile.domain,
        str(profile.min_working_tier),
        profile.requires_proxy,
        profile.required_proxy_type,
        profile.detected_waf,
        profile.success_count,
        profile.failure_count,
        profile.block_count,
        profile.avg_content_length,
        profile.stdev_content_length,
        last_success,
        last_block,
        datetime.fromtimestamp(profile.circuit_open_until, UTC)
        if profile.circuit_open_until
        else None,
        profile.climbs_above_floor,
        profile.blocks_at_floor,
        profile.circuit_opens,
        profile.avg_success_ms,
        profile.stdev_success_ms,
        profile.timed_success_count,
    )


# --------------------------------------------------------------------------
# The link graph
# --------------------------------------------------------------------------


def link_pairs(page_url: str, links: list[str]) -> dict[tuple[str, str], tuple[int, str]]:
    """A page's outbound links as domain -> domain counts, with one example.

    Self-links are dropped: a site linking to itself is navigation, and
    counting it as a referring domain would make every site its own biggest
    backer. `registrable_domain` decides what a domain IS — one definition,
    the same one link screening and cache keys use.
    """
    from engine.core.urls import registrable_domain

    source = registrable_domain(page_url)
    if not source:
        return {}

    out: dict[tuple[str, str], tuple[int, str]] = {}
    for link in links or []:
        if not isinstance(link, str) or not link.startswith(("http://", "https://")):
            continue
        target = registrable_domain(link)
        if not target or target == source:
            continue
        key = (source, target)
        count, sample = out.get(key, (0, link))
        out[key] = (count + 1, sample)
    return out


async def record_links(page_url: str, links: list[str]) -> int:
    """Fold one page's links into the graph. Returns the pairs written.

    `links` is a COUNT of distinct linking URLs seen from that source domain,
    so re-scraping the same page does not inflate it — the upsert takes the
    larger of the two rather than adding, because the same page seen twice is
    the same evidence twice.
    """
    pairs = link_pairs(page_url, links)
    if not pairs:
        return 0

    rows = [
        (source, target, count, sample_source, sample)
        for (source, target), (count, sample) in pairs.items()
        for sample_source in (page_url,)
    ]
    await db.executemany(
        """
        INSERT INTO domain_links (
            source_domain, target_domain, links, sample_source_url, sample_target_url
        ) VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (source_domain, target_domain) DO UPDATE SET
            links     = GREATEST(domain_links.links, EXCLUDED.links),
            last_seen = now()
        """,
        rows,
    )
    return len(rows)


async def backlinks(domain: str, limit: int = 100) -> list[dict[str, Any]]:
    """Which domains we have SEEN linking to this one, busiest first."""
    rows = await db.fetch(
        """
        SELECT source_domain, links, sample_source_url, sample_target_url,
               first_seen, last_seen
        FROM domain_links
        WHERE target_domain = $1
        ORDER BY links DESC, source_domain
        LIMIT $2
        """,
        domain,
        limit,
    )
    return [dict(r) for r in rows]


async def backlink_totals(domain: str) -> tuple[int, int]:
    """(referring domains, links seen) for a target domain."""
    row = await db.fetchrow(
        "SELECT count(*) AS domains, COALESCE(sum(links), 0) AS links "
        "FROM domain_links WHERE target_domain = $1",
        domain,
    )
    return (int(row["domains"]), int(row["links"])) if row else (0, 0)


async def outbound_totals(domain: str) -> tuple[int, int]:
    """The other direction: who this domain links OUT to. A site with 4,000
    outbound links and 3 inbound is a different animal from the reverse."""
    row = await db.fetchrow(
        "SELECT count(*) AS domains, COALESCE(sum(links), 0) AS links "
        "FROM domain_links WHERE source_domain = $1",
        domain,
    )
    return (int(row["domains"]), int(row["links"])) if row else (0, 0)


async def get_politeness(domain: str) -> tuple[int | None, int | None]:
    """This domain's OWN pacing, or (None, None) when it has never asked for any.

    Returning the global default here was indistinguishable from a domain that
    had genuinely been slowed to exactly that value, so the caller could never
    tell "no opinion" from "1000ms, deliberately" — and the plan-derived floor
    could never take effect on a domain nobody had touched.
    """
    row = await db.fetchrow(
        "SELECT politeness_delay_ms, max_concurrency FROM domain_profiles WHERE domain = $1",
        domain,
    )
    if row is None:
        return None, None
    return row["politeness_delay_ms"], row["max_concurrency"]


async def raise_politeness_delay(domain: str, delay_ms: int) -> None:
    """A 429's Retry-After raises the stored delay for the domain, and never
    lowers it."""
    await db.execute(
        """
        INSERT INTO domain_profiles (domain, politeness_delay_ms)
        VALUES ($1, $2)
        ON CONFLICT (domain) DO UPDATE
        SET politeness_delay_ms = GREATEST(domain_profiles.politeness_delay_ms, $2)
        """,
        domain,
        delay_ms,
    )


async def upsert_place(place: dict[str, Any]) -> None:
    """One row per Google feature id (engine/places). A repeat search refreshes
    the row and `last_seen_at`; it never duplicates. Only what the listing
    itself said is stored — enrichment results belong to leadgen's tables."""
    from engine.storage.ids import new_id

    await db.execute(
        """
        INSERT INTO places (
            id, feature_id, name, category, address, latitude, longitude,
            rating, review_count, place_url, website, phone,
            first_seen_at, last_seen_at, created_at, updated_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12, now(), now(), now(), now())
        ON CONFLICT (feature_id) DO UPDATE SET
            name         = EXCLUDED.name,
            category     = COALESCE(EXCLUDED.category, places.category),
            address      = COALESCE(EXCLUDED.address, places.address),
            latitude     = COALESCE(EXCLUDED.latitude, places.latitude),
            longitude    = COALESCE(EXCLUDED.longitude, places.longitude),
            rating       = COALESCE(EXCLUDED.rating, places.rating),
            review_count = COALESCE(EXCLUDED.review_count, places.review_count),
            place_url    = EXCLUDED.place_url,
            website      = COALESCE(EXCLUDED.website, places.website),
            phone        = COALESCE(EXCLUDED.phone, places.phone),
            last_seen_at = now(),
            updated_at   = now()
        """,
        new_id("place"),
        place["feature_id"],
        place["name"],
        place.get("category"),
        place.get("address"),
        place.get("latitude"),
        place.get("longitude"),
        place.get("rating"),
        place.get("review_count"),
        place["place_url"],
        place.get("website"),
        place.get("phone"),
    )


async def get_site_cookie(rule_host: str, name: str) -> str | None:
    """A consent cookie the engine harvested for itself (fetch/consent.py),
    keyed by registrable domain. Newer than the YAML default, so it wins."""
    row = await db.fetchrow(
        "SELECT value FROM site_cookies WHERE rule_host = $1 AND name = $2",
        rule_host,
        name,
    )
    return row["value"] if row else None


async def save_site_cookie(rule_host: str, name: str, value: str) -> None:
    await db.execute(
        """
        INSERT INTO site_cookies (rule_host, name, value, harvested_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (rule_host, name) DO UPDATE
            SET value = EXCLUDED.value, harvested_at = now()
        """,
        rule_host,
        name,
        value,
    )


async def cached_robots(host: str, ttl_hours: int = 24) -> str | None:
    """robots.txt for a HOST. Not a registrable domain — RFC 9309 scopes
    robots.txt to scheme+host+port, so blog.example.com and www.example.com
    are different documents."""
    row = await db.fetchrow(
        """
        SELECT body FROM robots_cache
        WHERE host = $1 AND fetched_at > now() - ($2 || ' hours')::interval
        """,
        host,
        str(ttl_hours),
    )
    return row["body"] if row else None


async def store_robots(host: str, body: str) -> None:
    """Cache robots.txt against its HOST.

    Previously written into `domain_profiles.domain`, which holds registrable
    domains everywhere else — so every `www.` prefix minted a phantom profile
    row with no learned signal in it, inflating every count over that table.
    """
    await db.execute(
        """
        INSERT INTO robots_cache (host, body, fetched_at)
        VALUES ($1, $2, now())
        ON CONFLICT (host) DO UPDATE
        SET body = EXCLUDED.body, fetched_at = now()
        """,
        host,
        body,
    )


# --------------------------------------------------------------------------
# Pages — storage AND cache
# --------------------------------------------------------------------------


async def cache_lookup(variant_hash: bytes, max_age_ms: int) -> asyncpg.Record | None:
    """The cache is this table. `maxAge` of 0 forces a fresh fetch.

    Keyed on the VARIANT, not the URL: this cache is shared across every
    customer, so a row only answers a request that asked for the same document —
    same exit country, same rendering. And only rows marked `shared_cacheable`,
    which excludes anything fetched with caller-supplied headers or cookies.
    """
    if max_age_ms <= 0:
        return None
    return await db.fetchrow(
        """
        SELECT * FROM pages
        WHERE variant_hash = $1 AND ok AND shared_cacheable
          AND fetched_at > now() - ($2 || ' milliseconds')::interval
        ORDER BY fetched_at DESC LIMIT 1
        """,
        variant_hash,
        str(max_age_ms),
    )


# Every column store_page may write. The query below is assembled from column
# names, so this allowlist is what keeps that assembly safe: a name that is not
# in here never reaches the SQL string. Values are always parameterised.
PAGE_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "job_id",
        "url",
        "source_url",
        "normalized_hash",
        "variant_hash",
        "shared_cacheable",
        "fetched_by",
        "status_code",
        "content_type",
        "content_hash",
        "markdown",
        "html",
        "raw_html",
        "links",
        "structured",
        "screenshot_path",
        "title",
        "description",
        "language",
        "author",
        "published_at",
        "page_type",
        "word_count",
        "extraction_confidence",
        "extraction_path",
        "fetch_tier",
        "tiers_attempted",
        "proxy_type",
        "proxy_bytes",
        "browser_ms",
        "ok",
        "error_code",
        "block_signals",
        "fetched_at",
        "expires_at",
    }
)


async def store_page(record: dict[str, Any]) -> str:
    """Upsert on (job_id, normalized_hash) — idempotent by design.

    A worker whose claim was reaped mid-write re-runs the same page; a blind
    insert would leave two rows and double the job's counters.
    """
    page = dict(record)
    page.setdefault("id", new_id("page"))

    unknown = set(page) - PAGE_COLUMNS
    if unknown:
        raise ValueError(f"unknown page columns: {sorted(unknown)}")

    columns = list(page)
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    column_sql = ", ".join(columns)
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in columns if c not in ("id", "job_id", "normalized_hash")
    )

    # S608 is suppressed on both branches because the interpolated fragments
    # are built exclusively from PAGE_COLUMNS, validated above — an unknown
    # column raises before any SQL is assembled. Every VALUE is bound as a
    # parameter and never interpolated.
    if page.get("job_id"):
        query = f"""
            INSERT INTO pages ({column_sql}) VALUES ({placeholders})
            ON CONFLICT (job_id, normalized_hash) WHERE job_id IS NOT NULL
            DO UPDATE SET {updates}
            RETURNING id
        """  # noqa: S608
    else:
        query = (
            f"INSERT INTO pages ({column_sql}) "  # noqa: S608
            f"VALUES ({placeholders}) RETURNING id"
        )

    result = await db.fetchval(query, *[page[c] for c in columns])
    return str(result)


async def get_page(page_id: str) -> asyncpg.Record | None:
    return await db.fetchrow("SELECT * FROM pages WHERE id = $1", page_id)


async def list_job_pages(
    job_id: str, *, after_id: str | None = None, limit: int = 50
) -> list[asyncpg.Record]:
    if after_id:
        return await db.fetch(
            """
            SELECT * FROM pages WHERE job_id = $1 AND id > $2
            ORDER BY id LIMIT $3
            """,
            job_id,
            after_id,
            limit,
        )
    return await db.fetch(
        "SELECT * FROM pages WHERE job_id = $1 ORDER BY id LIMIT $2", job_id, limit
    )


# --------------------------------------------------------------------------
# Change tracking
# --------------------------------------------------------------------------


async def record_version(
    normalized_hash: bytes,
    url: str,
    hash_value: bytes,
    word_count: int,
    owner_ref: str | None = None,
) -> tuple[str, Any]:
    """Append-only. Returns (`new` | `changed` | `same`, previous capture time).

    A row is written only when the hash differs from the latest, so the table
    holds one row per observed CHANGE rather than one per fetch. The history
    is the OWNER's: keyed by URL alone, one customer's check answered from
    another's, and the capture time told them when someone else had looked.
    """
    latest = await db.fetchrow(
        """
        SELECT content_hash, captured_at FROM page_versions
        WHERE normalized_hash = $1 AND owner_ref IS NOT DISTINCT FROM $2
        ORDER BY captured_at DESC LIMIT 1
        """,
        normalized_hash,
        owner_ref,
    )
    previous_at = latest["captured_at"] if latest else None

    if latest is None:
        status = "new"
    elif bytes(latest["content_hash"]) != hash_value:
        status = "changed"
    else:
        return "same", previous_at

    await db.execute(
        """
        INSERT INTO page_versions (normalized_hash, url, content_hash, word_count, owner_ref)
        VALUES ($1, $2, $3, $4, $5)
        """,
        normalized_hash,
        url,
        hash_value,
        word_count,
        owner_ref,
    )
    return status, previous_at


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


async def create_job(
    kind: str,
    api_key_id: str,
    payload: dict[str, Any],
    *,
    retention_days: int = 30,
    webhook_url: str | None = None,
    webhook_events: list[str] | None = None,
) -> str:
    job = new_id(kind)
    await db.execute(
        """
        INSERT INTO jobs (id, kind, api_key_id, input, expires_at, webhook_url, webhook_events)
        VALUES ($1, $2::job_kind, $3, $4::jsonb, $5, $6, $7)
        """,
        job,
        kind,
        api_key_id,
        payload,
        datetime.now(UTC) + timedelta(days=retention_days),
        webhook_url,
        webhook_events,
    )
    return job


async def get_job(job_id: str) -> asyncpg.Record | None:
    return await db.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)


async def set_job_status(job_id: str, status: str, error: dict[str, Any] | None = None) -> None:
    await db.execute(
        """
        UPDATE jobs SET
            status = $2::job_status,
            started_at = CASE WHEN $2 = 'running' AND started_at IS NULL
                              THEN now() ELSE started_at END,
            completed_at = CASE WHEN $2 IN ('completed','failed','cancelled')
                                THEN now() ELSE completed_at END,
            error = COALESCE($3::jsonb, error)
        WHERE id = $1
          -- Terminal states are immutable: a completed job never reopens.
          AND status NOT IN ('completed','failed','cancelled')
        """,
        job_id,
        status,
        error,
    )


async def skip_pending_frontier(job_id: str, reason: str) -> int:
    """Move every still-pending or -claimed frontier URL to `skipped`.

    A crawl that stops because it hit its `limit` leaves URLs it never fetched.
    They count in `total` but in no terminal bucket, so
    completed + failed + skipped < total and a caller cannot reconcile the run
    (the tally bug, in the limit-reached case). Landing them in
    `skipped` with a reason keeps every discovered URL in exactly one bucket.
    Returns how many were moved.
    """
    result = await db.execute(
        """
        UPDATE frontier
        SET status = 'skipped', skip_reason = $2
        WHERE job_id = $1 AND status IN ('pending', 'claimed')
        """,
        job_id,
        reason,
    )
    return int(result.split()[-1]) if result else 0


async def refresh_job_counters(job_id: str) -> None:
    """Counters are DERIVED from frontier state rather than incremented.

    Independent increments drift the moment a worker is reaped mid-update;
    deriving them removes that whole class of bug.
    """
    await db.execute(
        """
        UPDATE jobs j SET
            total     = f.total,
            completed = f.done,
            failed    = f.failed,
            skipped   = f.skipped
        FROM (
            SELECT
                count(*)                                      AS total,
                count(*) FILTER (WHERE status = 'done')       AS done,
                count(*) FILTER (WHERE status = 'failed')     AS failed,
                count(*) FILTER (WHERE status = 'skipped')    AS skipped
            FROM frontier WHERE job_id = $1
        ) f
        WHERE j.id = $1
        """,
        job_id,
    )


async def set_job_progress(job_id: str, stage: str, total: int, completed: int) -> None:
    """Where a Find Leads job is: its stage, and how far through it."""
    await db.execute(
        "UPDATE jobs SET stage = $2, total = $3, completed = $4 WHERE id = $1",
        job_id,
        stage,
        total,
        completed,
    )


async def store_lead_results(job_id: str, leads: list[dict[str, Any]]) -> None:
    """A run's leads, in the order shown. Replaces any earlier attempt's rows, so
    a job resumed after a restart never shows a lead twice."""
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM lead_results WHERE job_id = $1", job_id)
        await conn.executemany(
            "INSERT INTO lead_results (job_id, position, data) VALUES ($1, $2, $3::jsonb)",
            [(job_id, i, lead) for i, lead in enumerate(leads)],
        )


async def list_lead_results(job_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT data FROM lead_results WHERE job_id = $1 ORDER BY position", job_id
    )
    return [dict(r["data"]) for r in rows]


async def merge_job_cost(job_id: str, extra: dict[str, Any]) -> None:
    """Merge keys into a job's cost record (its credits, what they were for)."""
    await db.execute(
        "UPDATE jobs SET cost = COALESCE(cost, '{}'::jsonb) || $2::jsonb WHERE id = $1",
        job_id,
        extra,
    )


async def accumulate_job_cost(
    job_id: str, proxy_bytes: int, browser_ms: int, tier: str, credits: int = 0
) -> None:
    """Add one page's spend to the job's running total.

    `tier_breakdown` is SEEDED before it is written into. `jsonb_set` with a
    two-element path is a silent no-op when the parent key is absent —
    `create_missing` creates only the final key, and only if its parent already
    exists. Without the seed every tier count was discarded and no error was
    raised: across the whole database, not one job of either kind had ever
    recorded which rung fetched it (found 6 Sep 2026).
    """
    await db.execute(
        """
        UPDATE jobs SET cost = jsonb_set(
            jsonb_set(
                jsonb_set(
                    COALESCE(cost, '{}'::jsonb)
                        || jsonb_build_object(
                               'tier_breakdown',
                               COALESCE(cost->'tier_breakdown', '{}'::jsonb)
                           ),
                    '{proxy_bytes}',
                    to_jsonb(COALESCE((cost->>'proxy_bytes')::bigint, 0) + $2::bigint)
                ),
                '{browser_ms}',
                to_jsonb(COALESCE((cost->>'browser_ms')::bigint, 0) + $3::bigint)
            ),
            ARRAY['tier_breakdown', $4],
            to_jsonb(COALESCE((cost->'tier_breakdown'->>$4)::int, 0) + 1)
        ) || jsonb_build_object(
            'credits',
            COALESCE((cost->>'credits')::bigint, 0) + $5::bigint
        )
        WHERE id = $1
        """,
        job_id,
        proxy_bytes,
        browser_ms,
        tier,
        credits,
    )


# --------------------------------------------------------------------------
# Frontier
# --------------------------------------------------------------------------


async def add_frontier_urls(
    job_id: str,
    entries: list[dict[str, Any]],
) -> int:
    """Insert with ON CONFLICT DO NOTHING — dedup is the unique index's job."""
    if not entries:
        return 0
    rows = [
        (
            job_id,
            e["url"],
            e["url_hash"],
            e["normalized_hash"],
            e.get("depth", 0),
            e.get("parent_url"),
            e.get("discovered_via"),
            e.get("status", "pending"),
            e.get("skip_reason"),
        )
        for e in entries
    ]
    async with db.transaction() as conn:
        result = await conn.executemany(
            """
            INSERT INTO frontier (job_id, url, url_hash, normalized_hash, depth,
                                  parent_url, discovered_via, status, skip_reason)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::frontier_status,$9)
            ON CONFLICT (job_id, normalized_hash) DO NOTHING
            """,
            rows,
        )
    _ = result
    return len(rows)


async def claim_frontier_url(job_id: str, worker: str) -> asyncpg.Record | None:
    """Atomic claim. Breadth-first via ORDER BY depth, id.

    Shallow pages are usually the valuable ones, and a depth-first crawl that
    wanders into a calendar widget never returns.
    """
    return await db.fetchrow(
        """
        UPDATE frontier SET status='claimed', claimed_at=now(), claimed_by=$2,
                            attempts = attempts + 1
        WHERE id = (
          SELECT id FROM frontier
          WHERE job_id=$1 AND status='pending'
          ORDER BY depth, id
          FOR UPDATE SKIP LOCKED
          LIMIT 1
        )
        RETURNING *
        """,
        job_id,
        worker,
    )


async def complete_frontier_url(frontier_id: int, *, ok: bool) -> None:
    await db.execute(
        "UPDATE frontier SET status = $2::frontier_status WHERE id = $1",
        frontier_id,
        "done" if ok else "failed",
    )


async def reap_stale_claims(timeout_seconds: int = 300, max_attempts: int = 3) -> int:
    """Return rows claimed longer than the timeout to pending.

    At `max_attempts` the row goes to failed rather than looping for ever.
    """
    result = await db.execute(
        """
        UPDATE frontier SET
            status = CASE WHEN attempts >= $2 THEN 'failed'::frontier_status
                          ELSE 'pending'::frontier_status END,
            claimed_at = NULL,
            claimed_by = NULL
        WHERE status = 'claimed'
          AND claimed_at < now() - ($1 || ' seconds')::interval
        """,
        str(timeout_seconds),
        max_attempts,
    )
    return int(result.split()[-1]) if result else 0


async def release_claims(job_id: str, worker: str) -> int:
    """Hand this worker's claims on a job back to `pending` — now.

    Spec 07 §2: graceful shutdown must "release claimed frontier rows". It did
    not; they waited out the five-minute claim timeout instead. The attempt is
    given back too: a claim increments `attempts`, and a deploy is not a failed
    fetch — three restarts during one slow page would otherwise fail the URL at
    `max_attempts` without it ever having been tried and refused.
    """
    result = await db.execute(
        """
        UPDATE frontier SET status = 'pending', claimed_at = NULL, claimed_by = NULL,
                            attempts = GREATEST(attempts - 1, 0)
        WHERE job_id = $1 AND claimed_by = $2 AND status = 'claimed'
        """,
        job_id,
        worker,
    )
    return int(result.split()[-1]) if result else 0


async def frontier_row_count(job_id: str) -> int:
    """How many frontier rows a job has, in ANY state.

    "Has this job been seeded" is the question both runners need, and they were
    asking "does it have pending rows" instead. For a fresh job the two agree.
    For a RESUMED job they do not: a batch whose last URL finished but whose
    worker died before `_finish` has no pending rows, and would re-insert and
    re-scrape — and re-bill — every URL it had already done.
    """
    return int(await db.fetchval("SELECT count(*) FROM frontier WHERE job_id = $1", job_id) or 0)


async def orphaned_job_candidates(idle_seconds: int) -> list[asyncpg.Record]:
    """Active jobs with nothing claimed and no activity for `idle_seconds`.

    A CANDIDATE, not a verdict. A live worker between two URLs also has zero
    claims for a moment, so the caller must still rule out a job whose message
    is in the queue or that a live worker says it is driving. What this adds
    is the idle bound: a working crawl claims a URL every few seconds, and a
    single page is capped well inside the claim timeout.

    Only active jobs are grouped, so the frontier scan stays proportional to
    what is running rather than to everything ever crawled.
    """
    return await db.fetch(
        """
        SELECT j.id, j.kind, j.status, j.input, j.created_at, j.resumes,
               COALESCE(f.claimed, 0) AS claimed,
               COALESCE(f.pending, 0) AS pending,
               COALESCE(f.done, 0)    AS done,
               COALESCE(f.last_claim, j.created_at) AS last_activity
        FROM jobs j
        LEFT JOIN (
            SELECT job_id,
                   count(*) FILTER (WHERE status = 'claimed') AS claimed,
                   count(*) FILTER (WHERE status = 'pending') AS pending,
                   count(*) FILTER (WHERE status = 'done')    AS done,
                   max(claimed_at) AS last_claim
            FROM frontier
            WHERE job_id IN (SELECT id FROM jobs WHERE status IN ('queued', 'running'))
            GROUP BY job_id
        ) f ON f.job_id = j.id
        WHERE j.status IN ('queued', 'running')
          AND j.kind IN ('crawl', 'batch')
          AND COALESCE(f.claimed, 0) = 0
          AND COALESCE(f.last_claim, j.created_at) < now() - ($1 || ' seconds')::interval
        ORDER BY j.created_at
        """,
        str(idle_seconds),
    )


async def mark_job_resumed(job_id: str) -> int:
    return int(
        await db.fetchval(
            "UPDATE jobs SET resumes = resumes + 1 WHERE id = $1 RETURNING resumes", job_id
        )
        or 0
    )


async def fail_interrupted_job(job_id: str, message: str) -> None:
    """Drive a job that will not be resumed to an honest terminal state.

    Pages already collected stay listable. Every URL it never reached lands in
    `skipped` so completed + failed + skipped still equals total. Only ever
    called on a job with nothing claimed — `skip_pending_frontier` moves
    claimed rows as well, and would otherwise write off URLs a live worker was
    in the middle of fetching.
    """
    await skip_pending_frontier(job_id, "interrupted")
    await refresh_job_counters(job_id)
    await set_job_status(job_id, "failed", error={"code": "JOB_INTERRUPTED", "message": message})


async def frontier_counts(job_id: str) -> dict[str, int]:
    rows = await db.fetch(
        "SELECT status::text AS status, count(*) AS n FROM frontier WHERE job_id=$1 "
        "GROUP BY status",
        job_id,
    )
    return {row["status"]: row["n"] for row in rows}


async def pending_frontier_count(job_id: str) -> int:
    return (
        await db.fetchval(
            "SELECT count(*) FROM frontier WHERE job_id=$1 AND status='pending'", job_id
        )
        or 0
    )


# --------------------------------------------------------------------------
# Observability
# --------------------------------------------------------------------------


async def log_fetch_attempt(
    *,
    domain: str,
    url_hash: bytes,
    tier: str,
    outcome: str,
    job_id: str | None = None,
    status_code: int | None = None,
    latency_ms: int | None = None,
    bytes_transferred: int | None = None,
    proxy_id: str | None = None,
    block_signal: str | None = None,
) -> int | None:
    """One row per TIER ATTEMPT, not per request — this is what makes
    escalation waste visible. Returns the row id so a post-extraction verdict
    can amend the outcome it recorded."""
    return await db.fetchval(
        """
        INSERT INTO fetch_log (job_id, domain, url_hash, tier, outcome, status_code,
                               latency_ms, bytes, proxy_id, block_signal)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        RETURNING id
        """,
        job_id,
        domain,
        url_hash,
        tier,
        outcome,
        status_code,
        latency_ms,
        bytes_transferred,
        proxy_id,
        block_signal,
    )


async def amend_fetch_outcome(log_id: int, outcome: str, block_signal: str | None) -> None:
    """Correct a logged attempt once extraction has had its say.

    A 200 with a body is a transport success and is logged as one; the
    validator only rules on the CONTENT afterwards. Without this an operator
    auditing "why did dictionary.com fail" reads two successes and the caller's
    BLOCKED, with nothing joining them (measured, Sep 2026).
    """
    await db.execute(
        "UPDATE fetch_log SET outcome = $2, block_signal = $3 WHERE id = $1",
        log_id,
        outcome,
        block_signal,
    )


async def recent_domain_outcomes(domain: str, limit: int = 20) -> list[bool]:
    """The last `limit` outcomes for a domain, oldest first.

    Bounded to a day, not five minutes. The five-minute bound meant the
    breaker only ever saw a domain that failed FAST — three or four polite
    failures a minute never filled the window, and the breaker never opened
    on 542 consecutive failures. A day is long enough to see a slow failure
    and short enough that a domain that healed yesterday is not still judged
    by the week before.
    """
    rows = await db.fetch(
        """
        SELECT outcome FROM fetch_log
        WHERE domain = $1 AND recorded_at > now() - interval '24 hours'
        ORDER BY recorded_at DESC LIMIT $2
        """,
        domain,
        limit,
    )
    return [row["outcome"] == "success" for row in reversed(rows)]


async def recent_domain_outcomes_by_url(domain: str, limit: int = 20) -> list[tuple[bytes, bool]]:
    """The same window the breaker judges on, but saying WHICH url each
    outcome was for — so a domain-wide refusal can be told apart from one
    page failing over and over."""
    rows = await db.fetch(
        """
        SELECT url_hash, outcome FROM fetch_log
        WHERE domain = $1 AND recorded_at > now() - interval '24 hours'
        ORDER BY recorded_at DESC LIMIT $2
        """,
        domain,
        limit,
    )
    return [(r["url_hash"] or b"", r["outcome"] == "success") for r in reversed(rows)]


async def url_worked_recently(domain: str, digest: bytes) -> bool:
    """Has THIS url been read successfully in the breaker's own window?

    The breaker is keyed on the domain, which is right for protecting a host
    we are failing against and wrong for the caller: one hostile path takes
    the whole site down with it. Google's results page is the clearest case —
    it never yields, fills the failure window on its own, and everything else
    on google.com is refused behind it for fifteen minutes, then thirty, then
    an hour. A search page and a documentation page are not the same page.

    So a url with a success of its own in the window gets a probe rather than
    a refusal. It is bounded by that evidence: an unknown path on a domain we
    are failing against is still refused, which is what the breaker is for.
    """
    row = await db.fetchrow(
        """
        SELECT 1 FROM fetch_log
        WHERE domain = $1 AND url_hash = $2 AND outcome = 'success'
          AND recorded_at > now() - interval '24 hours'
        LIMIT 1
        """,
        domain,
        digest,
    )
    return row is not None


# --------------------------------------------------------------------------
# Retention (02-data-model.md s10)
# --------------------------------------------------------------------------


async def sweep_expired(batch: int = 1000) -> dict[str, int]:
    """Batched and LIMITed. An unbounded DELETE on a large table locks it and
    takes the API down with it."""
    counts: dict[str, int] = {}

    jobs = await db.execute(
        """
        DELETE FROM jobs WHERE id IN (
            SELECT id FROM jobs
            WHERE expires_at < now() AND status IN ('completed','failed','cancelled')
            LIMIT $1
        )
        """,
        batch,
    )
    counts["jobs"] = int(jobs.split()[-1]) if jobs else 0

    pages = await db.execute(
        """
        DELETE FROM pages WHERE id IN (
            SELECT id FROM pages
            WHERE (expires_at IS NOT NULL AND expires_at < now())
               OR fetched_at < now() - interval '90 days'
            LIMIT $1
        )
        """,
        batch,
    )
    counts["pages"] = int(pages.split()[-1]) if pages else 0

    # raw_html is the dominant storage cost and rarely needed after the fact:
    # null it at 7 days but keep the row.
    raw = await db.execute(
        """
        UPDATE pages SET raw_html = NULL WHERE id IN (
            SELECT id FROM pages
            WHERE raw_html IS NOT NULL AND fetched_at < now() - interval '7 days'
            LIMIT $1
        )
        """,
        batch,
    )
    counts["raw_html_nulled"] = int(raw.split()[-1]) if raw else 0

    logs = await db.execute(
        """
        DELETE FROM fetch_log WHERE id IN (
            SELECT id FROM fetch_log WHERE recorded_at < now() - interval '90 days' LIMIT $1
        )
        """,
        batch,
    )
    counts["fetch_log"] = int(logs.split()[-1]) if logs else 0

    return counts


# --------------------------------------------------------------------------
# Monitors
# --------------------------------------------------------------------------


async def api_key_by_id(key_id: str) -> ApiKey | None:
    """The same shape job_api_key builds, for work that has no job — a
    scheduled monitor check bills the key that created the monitor."""
    row = await db.fetchrow(
        """
        SELECT k.id, k.label, k.scopes, k.rate_limit_rpm, k.allow_js_exec, k.webhook_secret,
               k.active, k.owner_ref,
               COALESCE(o.credits_remaining, 0) AS credits_remaining,
               COALESCE(o.concurrency, 5) AS concurrency,
               COALESCE(o.suspended, false) AS suspended
        FROM api_keys k LEFT JOIN owners o ON o.owner_ref = k.owner_ref
        WHERE k.id = $1
        """,
        key_id,
    )
    if row is None:
        return None
    return ApiKey(
        id=row["id"],
        label=row["label"],
        scopes=list(row["scopes"]),
        rate_limit_rpm=row["rate_limit_rpm"],
        allow_js_exec=row["allow_js_exec"],
        webhook_secret=row["webhook_secret"],
        active=row["active"],
        owner_ref=row["owner_ref"],
        credits_remaining=row["credits_remaining"],
        concurrency=row["concurrency"],
        suspended=row["suspended"],
    )


async def create_monitor(
    api_key_id: str,
    *,
    name: str,
    urls: list[str],
    interval_minutes: int,
    goal: str | None,
    webhook_url: str | None,
    cap: int,
) -> str | None:
    """Insert only while the key is under `cap` active monitors — in ONE
    statement, so two concurrent creates cannot both pass a separate count
    (the security review caught the count-then-insert race). None means the
    cap held and nothing was written."""
    monitor_id = new_id("mon")
    row = await db.fetchrow(
        """
        INSERT INTO monitors (id, api_key_id, name, urls, interval_minutes, goal, webhook_url)
        SELECT $1, $2, $3, $4::jsonb, $5, $6, $7
        WHERE (SELECT count(*) FROM monitors WHERE api_key_id = $2 AND active) < $8
        RETURNING id
        """,
        monitor_id,
        api_key_id,
        name,
        urls,
        interval_minutes,
        goal,
        webhook_url,
        cap,
    )
    return str(row["id"]) if row else None


async def get_monitor(monitor_id: str) -> asyncpg.Record | None:
    return await db.fetchrow("SELECT * FROM monitors WHERE id = $1", monitor_id)


async def list_monitors(api_key_id: str) -> list[asyncpg.Record]:
    return await db.fetch(
        "SELECT * FROM monitors WHERE api_key_id = $1 ORDER BY created_at DESC", api_key_id
    )


async def set_monitor_active(monitor_id: str, active: bool) -> None:
    await db.execute("UPDATE monitors SET active = $2 WHERE id = $1", monitor_id, active)


async def delete_monitor(monitor_id: str) -> None:
    await db.execute("DELETE FROM monitors WHERE id = $1", monitor_id)


async def due_monitors(limit: int = 50) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT * FROM monitors
        WHERE active AND next_run_at <= now()
        ORDER BY next_run_at
        LIMIT $1
        """,
        limit,
    )


async def mark_monitor_run(monitor_id: str, interval_minutes: int) -> None:
    """Advance the schedule from NOW, not from the previous due time: a
    backlog after downtime must not fire every missed check at once."""
    await db.execute(
        """
        UPDATE monitors
        SET last_run_at = now(), next_run_at = now() + ($2 || ' minutes')::interval
        WHERE id = $1
        """,
        monitor_id,
        str(interval_minutes),
    )


async def insert_monitor_check(
    monitor_id: str,
    *,
    pages: list[dict[str, Any]],
    counts: dict[str, int],
    triggered_by: str,
) -> str:
    check_id = new_id("chk")
    await db.execute(
        """
        INSERT INTO monitor_checks
            (id, monitor_id, finished_at, pages, same, changed, new, errors, triggered_by)
        VALUES ($1, $2, now(), $3::jsonb, $4, $5, $6, $7, $8)
        """,
        check_id,
        monitor_id,
        pages,
        counts.get("same", 0),
        counts.get("changed", 0),
        counts.get("new", 0),
        counts.get("error", 0),
        triggered_by,
    )
    return check_id


async def list_monitor_checks(monitor_id: str, limit: int = 20) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT * FROM monitor_checks WHERE monitor_id = $1
        ORDER BY started_at DESC LIMIT $2
        """,
        monitor_id,
        limit,
    )


# --------------------------------------------------------------------------
# Content fingerprints — the same body served for different URLs
# --------------------------------------------------------------------------

# How long a fingerprint counts as evidence. A week is long enough to catch a
# domain that templates under load and short enough that a site which changed
# is not judged on last month's behaviour.
FINGERPRINT_WINDOW_DAYS = 7


async def record_content_fingerprint(
    domain: str, content_hash_value: bytes, url_hash: bytes
) -> int:
    """Record this body for this URL, and say how many DISTINCT URLs on this
    domain have produced the same body inside the window.

    Returns 1 for a normal page — itself and nothing else.
    """
    await db.execute(
        """
        INSERT INTO content_fingerprints (domain, content_hash, url_hash)
        VALUES ($1, $2, $3)
        ON CONFLICT (domain, content_hash, url_hash)
        DO UPDATE SET seen_at = now()
        """,
        domain,
        content_hash_value,
        url_hash,
    )
    count = await db.fetchval(
        f"""
        SELECT COUNT(DISTINCT url_hash) FROM content_fingerprints
        WHERE domain = $1 AND content_hash = $2
          AND seen_at >= now() - interval '{FINGERPRINT_WINDOW_DAYS} days'
        """,  # noqa: S608 - FINGERPRINT_WINDOW_DAYS is the int constant above, not input
        domain,
        content_hash_value,
    )
    return int(count or 1)


async def prune_content_fingerprints() -> int:
    """Drop fingerprints past the window. Called by the maintenance sweep."""
    status = await db.execute(
        f"""
        DELETE FROM content_fingerprints
        WHERE seen_at < now() - interval '{FINGERPRINT_WINDOW_DAYS} days'
        """  # noqa: S608 - FINGERPRINT_WINDOW_DAYS is the int constant above, not input
    )
    return int(status.split()[-1]) if status and status.split()[-1].isdigit() else 0
