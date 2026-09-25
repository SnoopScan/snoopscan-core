"""Common fetch interface (03-fetch-tiers.md section 1).

Every tier implements this protocol so the escalation controller can treat them
interchangeably, and so each is independently testable (principle P6).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from engine.core.models import Location, Tier
from engine.core.ssrf import ResolvedTarget

# <meta charset="…"> or <meta http-equiv="Content-Type" content="…; charset=…">
_META_CHARSET_RE = re.compile(r'<meta[^>]+charset=["\']?\s*([a-z0-9_\-]+)', re.IGNORECASE)


@dataclass
class FetchRequest:
    url: str
    target: ResolvedTarget | None = None
    timeout_ms: int = 30_000
    headers: dict[str, str] = field(default_factory=dict)
    # GET for every scrape. POST exists for the JSON APIs the lead-gen
    # pipeline reads (Product Hunt's GraphQL endpoint), which are cooperative
    # first-party sources — never for fetching a page.
    method: str = "GET"
    body: str | None = None
    mobile: bool = False
    location: Location | None = None
    proxy_url: str | None = None
    proxy_id: str | None = None
    proxy_type: str | None = None
    block_assets: bool = True
    wait_for_ms: int = 0
    # Record every request the page makes while it loads. Only the browser
    # rungs can honour it — the plain tiers load one document and run no
    # scripts — and it is off unless the caller asked for `network`, because
    # keeping the list costs memory and a settle wait nobody else needs.
    capture_network: bool = False
    # Per-site default cookies (site_rules.py): consent and preference state a
    # first visit would set. HTTP tiers send them as a Cookie header (already
    # folded into `headers`); browser tiers install them into the context.
    cookies: list[dict[str, str]] = field(default_factory=list)
    # The caller's interaction sequence, run inside the page before the HTML
    # is captured. Only the browser rungs can honour it. Held as the validated
    # Action models rather than dicts, so the executor reads guaranteed fields.
    actions: list[Any] = field(default_factory=list)
    captcha_handling: str = "auto"
    captcha_evidence: bool = False
    # Shared by request copies: a later fetch must not repeat an interaction.
    captcha_state: dict[str, Any] = field(default_factory=dict)
    # True when the FETCHER will choose the exit itself. The deep rungs mint
    # their own session per call, so a request handed to them legitimately
    # carries no proxy_url yet — and the egress policy, which cannot know
    # that, refused to build it. See _enforce_egress_policy.
    exit_chosen_by_fetcher: bool = False
    # Which grade of provider a rung that mints its OWN exit should draw from
    # (providers.pick). None is cost-first across every grade. The deep rungs
    # used to draw with no grade and no price, so the dearest pool carried the
    # most bytes.
    proxy_grade: str | None = None
    # The CALLER chose this exit's type (`proxy: datacenter`, say). A deep
    # rung may swap a datacenter exit the router chose for a residential one
    # of its own — a datacenter address at a fingerprinting rung is a wasted
    # load — but never one the caller asked for by name.
    proxy_pinned: bool = False
    # False when the proxy bandwidth budget is spent. The deep rungs then
    # do not mint an exit of their own: the budget used to be checked only
    # where the scrape service chose the exit, so a stealth rung kept buying
    # residential bytes on a day the cap had already refused everything else.
    paid_exit_allowed: bool = True
    # False for hosts whose players stream video however autoplay is set
    # (site_rules.yaml). The Firefox rungs then run without Media Source
    # Extensions, which is how a player fetches its segments. A preference,
    # not an intercepting route, so the deep rungs' fingerprint is untouched
    # everywhere else.
    media_streaming: bool = True

    def __post_init__(self) -> None:
        _enforce_egress_policy(self)


def wants_own_exit(req: FetchRequest) -> bool:
    """Should a deep rung mint a residential exit of its own for this request?

    Yes when the request brought no exit, or brought a DATACENTER one the
    router chose: the fingerprinting rungs exist for sites that refuse
    datacenter addresses, and a browser load through one is paid for and
    refused. No when the caller named the type, and no when the bandwidth
    budget is spent — the budget used to be checked only where the scrape
    service chose the exit, so these rungs kept buying on a day the cap had
    refused everything else.
    """
    if not req.paid_exit_allowed:
        return False
    if not req.proxy_url:
        return True
    return req.proxy_type == "datacenter" and not req.proxy_pinned


class DirectEgressRefused(RuntimeError):
    """A fetch would have left from this host's own address, and may not.

    Raised at construction rather than at send, so it is impossible to build a
    request that leaks the origin and then forget to check it.
    """


def _is_internal(host: str) -> bool:
    """Loopback and private addresses are our own services, not targets.

    SearXNG on 127.0.0.1 must never be dialled through a residential exit, and
    it is not an egress risk: nothing outside the host sees it.
    """
    if not host:
        return False
    bare = host.strip("[]").lower()
    if bare in {"localhost", "localhost.localdomain"} or bare.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(bare)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local


def _enforce_egress_policy(req: FetchRequest) -> None:
    """Refuse a direct fetch when the deployment forbids one.

    THE chokepoint. There are thirty-odd places that build a FetchRequest —
    platform shortcuts, robots, sitemaps, search providers, map, the lead
    pipeline — and only the scrape path consults the proxy selector. Enforcing
    the policy at each caller would be a policy with thirty holes in it; a
    request that carries no proxy simply cannot be constructed when direct
    egress is off.

    Why it matters: the address customers connect to and the address targets
    see must not be the same one. One abusive customer puts it on a WAF
    reputation list and the cheap tiers stop working for everybody, abuse
    reports arrive at the host running the API, and any caller can read the
    address back by scraping an echo service.
    """
    from engine.settings import settings

    if req.proxy_url or settings.allow_direct_egress:
        return
    if req.exit_chosen_by_fetcher:
        # Not a direct fetch: the rung about to run it picks a fresh exit of
        # its own. Without this, retrying a challenge on a new IP — which
        # works by dropping the refused proxy — raised DirectEgressRefused and
        # killed the retry path on every deployment that forbids direct
        # egress, i.e. every production one.
        return
    host = urlsplit(req.url).hostname or ""
    if _is_internal(host):
        return
    raise DirectEgressRefused(
        f"a direct fetch of {host or req.url!r} was refused: this deployment "
        "does not permit fetching from its own address (allow_direct_egress)"
    )


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    status_code: int | None
    headers: dict[str, str]
    body: bytes
    content_type: str | None
    tier: str
    latency_ms: int
    bytes_transferred: int  # MEASURED, not estimated from body length
    proxy_id: str | None = None
    proxy_type: str | None = None
    browser_ms: int = 0
    error: str | None = None
    # The page's own traffic, when `capture_network` was asked for and this
    # rung could honour it. None means NOT CAPTURED — which is different from
    # an empty list, a page that made no requests at all.
    network: list[dict[str, Any]] | None = None
    network_seen: int = 0
    # Screenshots, scrapes and JS returns the action sequence produced.
    action_results: dict[str, Any] | None = None
    # Set when one of the CALLER's steps failed. Kept apart from `error`,
    # which means the transport failed: a selector that matches nothing is
    # not a network fault, must not be retried (it will fail identically and
    # bill twice), and must not be reported as one.
    action_error: str | None = None
    # "invalid" (the step cannot be carried out as written) or "timeout" (it
    # was well-formed and the page did not answer). Different diagnoses.
    action_fault: str | None = None
    # A TERMINAL refusal by the fetcher itself — "response_too_large",
    # "binary_content". Not a transport error, which the ladder retries and
    # climbs on: a file refused at tier 0 for its size would otherwise be
    # downloaded again, whole, by every rung above it. The validator reads it
    # as a target error, and the ladder stops.
    refused: str | None = None
    refused_detail: str | None = None

    @property
    def ok_transport(self) -> bool:
        """Transport-level success only. Says nothing about whether the body is
        real content — that is the block detector's job (principle P2)."""
        return self.error is None and self.status_code is not None

    def text(self, limit: int | None = None) -> str:
        raw = self.body[:limit] if limit else self.body
        encoding = None
        if self.content_type and "charset=" in self.content_type.lower():
            encoding = self.content_type.lower().split("charset=", 1)[1].split(";")[0].strip()
        if not encoding:
            # No charset in the header — common — so read the document's own
            # declaration. Assuming UTF-8 turned every windows-1252 page's
            # apostrophes and pound signs into replacement characters.
            head = raw[:4096].decode("ascii", errors="ignore").lower()
            m = _META_CHARSET_RE.search(head)
            encoding = m.group(1) if m else "utf-8"
        try:
            return raw.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return raw.decode("utf-8", errors="replace")


@runtime_checkable
class Fetcher(Protocol):
    name: str

    async def fetch(self, req: FetchRequest) -> FetchResult: ...

    async def healthcheck(self) -> bool: ...


# Minimum wall-clock a tier needs to be worth starting. Below this the
# escalation controller stops rather than beginning an attempt that will time
# out and cost money for nothing.
MIN_TIER_TIME_MS: dict[Tier, int] = {
    Tier.HTTP: 2_000,
    Tier.IMPERSONATE: 2_000,
    Tier.BROWSER: 8_000,
    Tier.STEALTH: 15_000,
    Tier.STEALTH_HARD: 20_000,
    Tier.MOBILE: 20_000,
}

# Order-of-magnitude relative cost, for reporting and for deciding whether an
# escalation is worth it. Tier 3 is ~100x tier 1.
TIER_RELATIVE_COST: dict[Tier, float] = {
    Tier.HTTP: 1.0,
    Tier.IMPERSONATE: 1.2,
    Tier.BROWSER: 40.0,
    Tier.STEALTH: 120.0,
    Tier.STEALTH_HARD: 200.0,
    Tier.MOBILE: 400.0,
}
