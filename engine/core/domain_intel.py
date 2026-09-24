"""What is known about a DOMAIN, as opposed to a page.

SnoopScan reads pages. It has never been able to answer anything about the
domain underneath one — how old it is, who runs it, who links to it — and a
colleague ran into the wall from the other side on 9 Sep 2026, comparing our
prank pages against the sites outranking them:

    "SnoopScan reads pages, not link graphs — no backlinks, domain age or
    authority. Given we win every on-page signal and still lose, off-page is
    the most likely real gap."

They were right, and it took thirty seconds of RDAP to see it: the domain
beating us was registered in 2000 on a ten-year registration, and ours is
months old. No amount of on-page work answers that.

Two free sources, no key and no vendor between us and the answer:

    RDAP    the protocol that replaced WHOIS. JSON over HTTPS, one request,
            gives registration and expiry dates, the registrar and the
            nameservers. Domain AGE is a subtraction.
    DNS     A/AAAA/MX/NS/TXT, which is where hosting, mail provider and CDN
            come from.

Both are deliberately separate from the fetch ladder: neither touches the
target's web server, so neither can be blocked, rate-limited or fingerprinted,
and neither needs a proxy.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from engine.core.errors import InvalidRequest
from engine.settings import settings

logger = structlog.get_logger(__name__)

# A hostname, and nothing that could climb out of the URL path we put it in.
# RDAP takes the domain as a path segment, so this is a boundary, not a
# nicety: `../` or a scheme here would address a different endpoint entirely.
_HOSTNAME = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?:\.(?!-)[a-z0-9-]{1,63})+$")

# The registrar's own dates, in the order RDAP names them.
_REGISTERED = ("registration", "reregistration")
_EXPIRES = ("expiration",)
_UPDATED = ("last changed", "last update of RDAP database")

RECORD_TYPES = ("A", "AAAA", "MX", "NS", "TXT")


@dataclass(frozen=True)
class Registration:
    createdAt: str | None = None
    expiresAt: str | None = None
    updatedAt: str | None = None
    # The number an SEO question is actually asking. Derived, not reported:
    # every caller was going to subtract these two dates themselves.
    ageDays: int | None = None
    registrar: str | None = None
    nameservers: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Dns:
    a: list[str] = field(default_factory=list)
    aaaa: list[str] = field(default_factory=list)
    mx: list[str] = field(default_factory=list)
    ns: list[str] = field(default_factory=list)
    txt: list[str] = field(default_factory=list)


def normalise(value: str) -> str:
    """A bare registrable hostname from a URL, a host, or something wrong.

    Callers pass all three — `https://example.com/path`, `www.example.com`,
    `example.com` — and a domain endpoint that only accepted one of them would
    be answering a question nobody asked.
    """
    from engine.core.urls import registrable_domain

    candidate = (value or "").strip().lower()
    if not candidate:
        raise InvalidRequest("A domain or URL is required.")
    candidate = registrable_domain(candidate) or candidate
    candidate = candidate.split("/")[0].split("@")[-1].split(":")[0].strip(".")

    if not _HOSTNAME.match(candidate):
        raise InvalidRequest(f"{value!r} is not a domain. Pass a hostname or an absolute URL.")
    return candidate


def _vcard_name(entity: dict[str, Any]) -> str | None:
    """The `fn` field out of RDAP's jCard, which is an array of arrays."""
    vcard = entity.get("vcardArray")
    if not isinstance(vcard, list) or len(vcard) < 2:
        return None
    for item in vcard[1]:
        if isinstance(item, list) and len(item) >= 4 and item[0] == "fn":
            return str(item[3]) or None
    return None


def _event(events: list[dict[str, Any]], names: tuple[str, ...]) -> str | None:
    for name in names:
        for event in events:
            if str(event.get("eventAction", "")).lower() == name:
                date = event.get("eventDate")
                if date:
                    return str(date)
    return None


def _age_days(created: str | None) -> int | None:
    if not created:
        return None
    try:
        when = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - when).days)


def parse_rdap(payload: dict[str, Any]) -> Registration:
    """RDAP's own shape into ours. Pure, so it can be tested off a capture."""
    events = [e for e in (payload.get("events") or []) if isinstance(e, dict)]
    created = _event(events, _REGISTERED)

    registrar = None
    for entity in payload.get("entities") or []:
        if isinstance(entity, dict) and "registrar" in (entity.get("roles") or []):
            registrar = _vcard_name(entity)
            break

    nameservers = sorted(
        {
            str(ns.get("ldhName", "")).lower()
            for ns in (payload.get("nameservers") or [])
            if isinstance(ns, dict) and ns.get("ldhName")
        }
    )

    return Registration(
        createdAt=created,
        expiresAt=_event(events, _EXPIRES),
        updatedAt=_event(events, _UPDATED),
        ageDays=_age_days(created),
        registrar=registrar,
        nameservers=nameservers,
        statuses=[str(s) for s in (payload.get("status") or [])],
    )


async def registration(domain: str, client: httpx.AsyncClient | None = None) -> Registration | None:
    """RDAP for one domain, or None when the registry has no record of it.

    None means "not registered, or this TLD publishes no RDAP" — which is a
    real answer and a different one from an error. A registry that is slow or
    refusing is logged and also returns None rather than failing the whole
    request: the DNS half is still worth having.
    """
    host = normalise(domain)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True)
    try:
        response = await client.get(
            f"{settings.rdap_base_url.rstrip('/')}/domain/{host}",
            headers={"Accept": "application/rdap+json"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return parse_rdap(response.json())
    except Exception as exc:  # noqa: BLE001 - a registry outage is not our caller's fault
        logger.info("rdap_unavailable", domain=host, error=str(exc))
        return None
    finally:
        if owns:
            await client.aclose()


async def dns_records(domain: str, types: tuple[str, ...] = RECORD_TYPES) -> Dns:
    """The records that say where a domain lives and who handles its mail.

    Every type is asked for at once and a missing one is an empty list, not an
    error: almost no domain has all five, and the absence of MX is itself a
    fact worth returning.
    """
    import dns.asyncresolver
    import dns.exception

    host = normalise(domain)
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 8.0

    async def one(record: str) -> tuple[str, list[str]]:
        try:
            answer = await resolver.resolve(host, record)
        except dns.exception.DNSException:
            return record, []
        values = []
        for item in answer:
            text = item.to_text().strip('"')
            values.append(text)
        return record, sorted(set(values))

    found = dict(await asyncio.gather(*(one(t) for t in types)))
    return Dns(
        a=found.get("A", []),
        aaaa=found.get("AAAA", []),
        mx=found.get("MX", []),
        ns=found.get("NS", []),
        txt=found.get("TXT", []),
    )
