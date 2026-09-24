"""Google results pages, bought: rankings and the AI Overview, per country.

We cannot fetch Google's results page ourselves. Plain HTTP gets a page that
demands JavaScript; every browser rung, through residential exits in the
country asked for, gets Google's unusual-traffic wall (measured, Sep 2026). A
bought results page is the only dependable source, so this is a thin, typed
client around one provider, DataForSEO, configured with the operator's own
credentials and absent without them.

What it adds beyond the raw results is the monitoring question itself: for a
given domain, where does it rank, and does the AI Overview cite it? Both are
answered from the same call.

The AI Overview is optional because it doubles the provider's price for that
call; the provider refunds the extra when no Overview loads.
"""

from __future__ import annotations

import base64
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.core.errors import InvalidRequest, SerpUnavailable
from engine.core.urls import registrable_domain
from engine.settings import settings

ENDPOINT = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
TIMEOUT_S = 90.0

# Google's geographic target IDs for countries are 2000 + the ISO 3166-1
# numeric code (the provider's own example: United States, 840 -> 2840). The
# main markets are listed; anything else can be passed as `locationCode`.
_ISO_NUMERIC: dict[str, int] = {
    "us": 840, "gb": 826, "ca": 124, "au": 36, "nz": 554, "ie": 372,
    "de": 276, "fr": 250, "es": 724, "it": 380, "nl": 528, "be": 56,
    "ch": 756, "at": 40, "se": 752, "no": 578, "dk": 208, "fi": 246,
    "pl": 616, "pt": 620, "cz": 203, "gr": 300, "ro": 642, "hu": 348,
    "ua": 804, "tr": 792, "il": 376, "ae": 784, "sa": 682, "eg": 818,
    "za": 710, "ng": 566, "ke": 404, "in": 356, "jp": 392, "kr": 410,
    "sg": 702, "hk": 344, "tw": 158, "ph": 608, "id": 360, "my": 458,
    "th": 764, "vn": 704, "br": 76, "mx": 484, "ar": 32, "cl": 152,
    "co": 170,
}  # fmt: skip


def location_code(country: str) -> int:
    numeric = _ISO_NUMERIC.get(country.lower())
    if numeric is None:
        raise InvalidRequest(
            f"No location code known for country {country!r}; pass `locationCode` "
            "(Google's geographic target ID) instead",
            {"field": "country"},
        )
    return 2000 + numeric


class SerpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(
        min_length=1,
        max_length=700,
        description="The search to look up, as it would be typed into Google.",
    )
    country: str = Field(
        default="us",
        min_length=2,
        max_length=2,
        description="Two-letter code of the country whose results you want, e.g. `gb`.",
    )
    locationCode: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Google's own geographic target ID, for somewhere finer than a country. Overrides "
            "`country`."
        ),
    )
    language: str = Field(
        default="en",
        min_length=2,
        max_length=10,
        description="The language code of the results, e.g. `en`.",
    )
    device: Literal["desktop", "mobile"] = Field(
        default="desktop", description="`desktop` or `mobile`."
    )
    # Results to read. Above 10 the provider may charge per extra page.
    depth: int = Field(default=10, ge=1, le=100, description="How many results to read, 1 to 100.")
    aiOverview: bool = Field(
        default=True,
        description="Include Google's AI Overview and the pages it cites. On by default.",
    )
    # The site being monitored. When given, the answer says where it ranks
    # and whether the AI Overview cites it.
    domain: str | None = Field(
        default=None,
        max_length=253,
        description=(
            "Your site. When given, the answer says where it ranks and whether the AI Overview "
            "cites it."
        ),
    )

    @field_validator("domain")
    @classmethod
    def _bare_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        bare = value.strip().lower()
        for prefix in ("https://", "http://"):
            bare = bare.removeprefix(prefix)
        return bare.split("/", 1)[0] or None


def configured() -> bool:
    return bool(settings.dataforseo_login and settings.dataforseo_password)


def task_body(req: SerpRequest) -> list[dict[str, Any]]:
    """The provider's task array: one task per live call."""
    task: dict[str, Any] = {
        "keyword": req.keyword,
        "location_code": req.locationCode or location_code(req.country),
        "language_code": req.language,
        "device": req.device,
        "depth": req.depth,
    }
    if req.aiOverview:
        task["load_async_ai_overview"] = True
    return [task]


def _auth_header() -> str:
    pair = f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    return "Basic " + base64.b64encode(pair).decode()


def _same_site(url_or_domain: str | None, domain: str) -> bool:
    if not url_or_domain:
        return False
    host = url_or_domain
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].lower()
    return registrable_domain(f"https://{host}/") == registrable_domain(f"https://{domain}/")


def _references(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Every page the AI Overview cited, from the item and its elements, once each."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    pools = [item.get("references") or []]
    for element in item.get("items") or []:
        if isinstance(element, dict):
            pools.append(element.get("references") or [])
    for pool in pools:
        for ref in pool:
            if not isinstance(ref, dict):
                continue
            url = str(ref.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            found.append(
                {
                    "url": url,
                    "domain": ref.get("domain"),
                    "title": ref.get("title"),
                    "source": ref.get("source"),
                }
            )
    return found


def parse(payload: dict[str, Any], req: SerpRequest) -> dict[str, Any]:
    """The provider's response, reduced to what a monitor reads."""
    tasks = payload.get("tasks") or []
    task = tasks[0] if tasks else {}
    if int(task.get("status_code") or 0) != 20000:
        raise SerpUnavailable(
            f"The results provider refused the request: {task.get('status_message') or 'no reason'}"
        )
    results = task.get("result") or []
    result = results[0] if results else {}
    items = [i for i in (result.get("items") or []) if isinstance(i, dict)]

    organic = [
        {
            "position": i.get("rank_absolute"),
            "rank": i.get("rank_group"),
            "url": i.get("url"),
            "domain": i.get("domain"),
            "title": i.get("title"),
            "description": i.get("description"),
        }
        for i in items
        if i.get("type") == "organic"
    ]

    overview_item = next((i for i in items if i.get("type") == "ai_overview"), None)
    overview: dict[str, Any] = {"present": overview_item is not None}
    if overview_item is not None:
        overview["text"] = overview_item.get("markdown") or " ".join(
            str(e.get("text") or "")
            for e in overview_item.get("items") or []
            if isinstance(e, dict) and e.get("text")
        )
        overview["references"] = _references(overview_item)

    out: dict[str, Any] = {
        "keyword": result.get("keyword") or req.keyword,
        "country": req.country.lower(),
        "locationCode": req.locationCode or location_code(req.country),
        "device": req.device,
        "organic": organic,
        "aiOverview": overview,
        "itemTypes": result.get("item_types") or [],
        "checkUrl": result.get("check_url"),
    }

    if req.domain:
        ranked = next((o for o in organic if _same_site(o["url"], req.domain)), None)
        cited = [r for r in overview.get("references", []) if _same_site(r["url"], req.domain)]
        out["domain"] = {
            "domain": req.domain,
            "position": ranked["position"] if ranked else None,
            "url": ranked["url"] if ranked else None,
            "citedInAiOverview": bool(cited),
            "citations": cited,
        }
    return out


async def fetch(req: SerpRequest, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """One live results page. SerpUnavailable if unconfigured or refused."""
    # The caller's own mistakes first: a bad country is a 400 whatever this
    # deployment has configured, not a 503 that hides it until later.
    body = task_body(req)
    if not configured():
        raise SerpUnavailable(
            "Results-page monitoring is not configured on this deployment "
            "(set ENGINE_DATAFORSEO_LOGIN and ENGINE_DATAFORSEO_PASSWORD)"
        )
    owns = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT_S)
    try:
        response = await http.post(
            ENDPOINT,
            json=body,
            headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
        )
    except httpx.HTTPError as exc:
        raise SerpUnavailable(
            f"The results provider could not be reached: {type(exc).__name__}"
        ) from exc
    finally:
        if owns:
            await http.aclose()
    if response.status_code == 401:
        raise SerpUnavailable("The results provider rejected our credentials")
    if response.status_code >= 400:
        raise SerpUnavailable(f"The results provider answered {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SerpUnavailable("The results provider sent a response we could not read") from exc
    return parse(payload, req)
