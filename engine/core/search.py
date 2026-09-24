"""Search, as a ladder of providers (07-orchestration.md section 8).

`/v1/search` walks the configured rungs and takes the first that answers, so one
provider refusing us costs a little latency instead of the whole endpoint.

**SearXNG (the default first rung).** A SearXNG you host, asked for JSON. It leads
because it spreads one query across many engines at once and because its per-engine
parsers are somebody else's full-time job. Requires `searxng_url`.

**DuckDuckGo (the fallback).** `lite.duckduckgo.com` publishes `Allow: /` for every
user agent and answers our own impersonation tier, so it costs nothing to run.
Results are Bing-derived.

**Serper (optional).** Google results, rented. Worth the money for rank-sensitive
work; a key switches it on.

With the provider explicitly set to `none` the endpoint returns a clear 503. It does
NOT return an empty result set: an agent told "no results" will conclude the thing
does not exist, which is a different and worse answer than "search is unavailable".
The same distinction governs the walk — see `search()`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from engine.settings import settings

logger = structlog.get_logger(__name__)

# Repeated identical searches in one session are common and each costs money.
SEARCH_CACHE_TTL_MS = 3_600_000


@dataclass
class SearchResult:
    url: str
    title: str | None = None
    description: str | None = None
    position: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "position": self.position,
        }


@dataclass(frozen=True)
class SearchQuery:
    """One search, and everything the caller asked for about it.

    `demands()` is the load-bearing part. A parameter the caller SET is a
    requirement; a provider that cannot honour it is dropped from the ladder
    rather than answering with something else. This is the same rule the proxy
    layer learned the hard way: silently giving someone a different thing from
    the one they asked for is worse than telling them you cannot.
    """

    query: str
    limit: int = 10
    country: str | None = None
    place: str | None = None
    language: str | None = None
    device: str | None = None
    freshness: str | None = None
    safe_search: str | None = None
    page: int = 1
    auto_correct: bool = True

    def demands(self) -> frozenset[str]:
        """The optional capabilities this query actually requires."""
        asked: set[str] = set()
        if self.country:
            asked.add("country")
        if self.place:
            asked.add("place")
        if self.language:
            asked.add("language")
        if self.device:
            asked.add("device")
        if self.freshness:
            asked.add("freshness")
        if self.safe_search:
            asked.add("safe_search")
        if self.page > 1:
            asked.add("page")
        if not self.auto_correct:
            asked.add("auto_correct")
        return frozenset(asked)


@dataclass
class SearchResponse:
    """Results, plus the two things every engine returns and most wrappers bin.

    `related` is the engine's own "searches related to this" — free on the same
    response, and what an agent needs to widen a search it got wrong. Throwing
    it away means the caller pays for a second search to rediscover it.

    There is deliberately no people-also-ask field. Scrapingdog returns a
    `peopleAlsoAskedFor` block, but inspected against a real response it holds
    `{link, displayed_link, rank}` and no question text at all — it is the list
    of sources cited in that box, not the questions. A field named for questions
    that contains links is worse than no field.
    """

    results: list[SearchResult] = field(default_factory=list)
    provider: str = "none"
    cached: bool = False
    related: list[str] = field(default_factory=list)
    paid: bool = False


class SearchUnavailable(Exception):
    """No SERP vendor is configured, or the vendor is down.

    Distinct from an empty result set on purpose.
    """


class SerpProvider(Protocol):
    """A rung. `supports` is a promise, and the ladder holds it to it."""

    name: str
    supports: frozenset[str]
    # Whether answering costs us money. The caller is billed on the rung that
    # actually answered, not on the endpoint they called.
    paid: bool

    async def search(self, q: SearchQuery) -> list[SearchResult]: ...


class NullProvider:
    """The default. Refuses honestly rather than inventing results."""

    name = "none"
    paid = False
    supports: frozenset[str] = frozenset()

    async def search(self, q: SearchQuery) -> list[SearchResult]:
        _ = q
        raise SearchUnavailable(
            "No search provider is configured. Set ENGINE_SEARCH_PROVIDER and its "
            "API key to enable /v1/search."
        )


class SerperProvider:
    """Serper.dev — a common, cheap SERP vendor with a simple JSON API.

    Written to the adapter interface so swapping vendors is a config change.
    Nothing about the rest of the engine knows which one is in use.
    """

    name = "serper"
    paid = True
    endpoint = "https://google.serper.dev/search"
    # Serper's documented knobs. `tbs` and `page` exist; `device` does not.
    supports = frozenset({"country", "language", "page", "freshness", "auto_correct"})

    def __init__(self, api_key: str, fetcher: Any = None) -> None:
        self._api_key = api_key
        self._fetcher = fetcher

    # Serper's own names for our freshness values.
    TBS = {"hour": "qdr:h", "day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}

    async def search(self, q: SearchQuery) -> list[SearchResult]:
        from engine.core.fetch.base import FetchRequest
        from engine.core.fetch.tier0_http import HttpFetcher

        fetcher = self._fetcher or HttpFetcher()
        limit = q.limit
        payload: dict[str, Any] = {"q": q.query, "num": min(limit, 100)}
        if q.country:
            payload["gl"] = q.country.lower()
        if q.language:
            payload["hl"] = q.language
        if q.page > 1:
            payload["page"] = q.page
        if q.freshness:
            payload["tbs"] = self.TBS[q.freshness]
        if not q.auto_correct:
            payload["autocorrect"] = False

        result = await fetcher.fetch(
            FetchRequest(
                url=self.endpoint,
                method="POST",
                body=json.dumps(payload),
                headers={
                    "X-API-KEY": self._api_key,
                    "Content-Type": "application/json",
                },
                timeout_ms=20_000,
            )
        )
        if result.status_code != 200:
            raise SearchUnavailable(f"The search provider returned HTTP {result.status_code}.")
        try:
            body = json.loads(result.text())
        except (ValueError, json.JSONDecodeError) as exc:
            raise SearchUnavailable("The search provider returned invalid JSON.") from exc

        out: list[SearchResult] = []
        for position, item in enumerate(body.get("organic", [])[:limit], start=1):
            if not isinstance(item, dict) or not item.get("link"):
                continue
            out.append(
                SearchResult(
                    url=item["link"],
                    title=item.get("title"),
                    description=item.get("snippet"),
                    position=position,
                )
            )
        return out


class DuckDuckGoProvider:
    """DuckDuckGo's lite endpoint, read with the engine's own impersonation tier.

    The lite page is a plain table of results, which is why it survives a
    fingerprint that a full SERP would refuse. Each result link is wrapped in a
    DuckDuckGo redirect carrying the real URL in `uddg`; we unwrap it so an
    agent is handed the destination, not the tracker.

    One page per query — enough for the agent's job of choosing what to read.
    """

    name = "duckduckgo"
    paid = False
    endpoint = "https://lite.duckduckgo.com/lite/"
    # The lite endpoint takes a region and nothing else. Declaring more would
    # make the ladder route work here that quietly comes back wrong.
    supports = frozenset({"country"})

    # DuckDuckGo's region codes. Anything unmapped searches without a region.
    REGIONS = {
        "us": "us-en",
        "gb": "uk-en",
        "uk": "uk-en",
        "de": "de-de",
        "fr": "fr-fr",
        "nl": "nl-nl",
        "es": "es-es",
        "it": "it-it",
        "ca": "ca-en",
        "au": "au-en",
        "jp": "jp-jp",
        "br": "br-pt",
        "in": "in-en",
        "ie": "ie-en",
        "nz": "nz-en",
    }

    def __init__(self, fetcher: Any = None) -> None:
        self._fetcher = fetcher

    async def search(self, q: SearchQuery) -> list[SearchResult]:
        from urllib.parse import urlencode

        from engine.core.fetch.base import FetchRequest
        from engine.core.fetch.tier1_impersonate import ImpersonateFetcher

        fetcher = self._fetcher or ImpersonateFetcher()
        limit = q.limit
        params = {"q": q.query, "kl": self.REGIONS.get((q.country or "").lower(), "wt-wt")}
        url = f"{self.endpoint}?{urlencode(params)}"
        try:
            result = await fetcher.fetch(FetchRequest(url=url, timeout_ms=20_000))
            # One retry on a transport miss — no status at all, not a refusal.
            # Measured 17 Sep 2026 on the production box: the first lite fetch
            # came back with no status and the next five answered 200 with ten
            # rows each. Two such misses inside a window trip the breaker for
            # three minutes, and on a one-rung ladder that is a full search
            # outage caused by a single dropped connection.
            if result.status_code is None:
                result = await fetcher.fetch(FetchRequest(url=url, timeout_ms=20_000))
        except Exception as exc:  # a transport failure is an outage, not an empty web
            raise SearchUnavailable("Could not reach the search provider.") from exc
        if result.status_code != 200:
            raise SearchUnavailable(f"The search provider returned HTTP {result.status_code}.")

        return self.parse(result.text())[:limit]

    @staticmethod
    def parse(html: str) -> list[SearchResult]:
        """Rows in document order: a link starts a result, a snippet joins the last one."""
        from selectolax.parser import HTMLParser

        out: list[SearchResult] = []
        for row in HTMLParser(html).css("tr"):
            link = row.css_first("a.result-link")
            if link is not None:
                url = DuckDuckGoProvider.unwrap(link.attributes.get("href") or "")
                title = link.text(strip=True) or None
                if url:
                    out.append(SearchResult(url=url, title=title, position=len(out) + 1))
                continue
            snippet = row.css_first("td.result-snippet")
            if snippet is not None and out:
                out[-1].description = snippet.text(strip=True) or None

        return out

    @staticmethod
    def unwrap(href: str) -> str | None:
        """The destination behind DuckDuckGo's redirect, or None if this is not a result."""
        from urllib.parse import parse_qs, urlsplit

        if href.startswith("//"):
            href = f"https:{href}"
        parts = urlsplit(href)
        target = parse_qs(parts.query).get("uddg", [None])[0]
        if target and target.startswith(("http://", "https://")):
            return target
        # An unwrapped absolute link is a result too, unless it points back at DuckDuckGo
        # (their own ads and settings pages do).
        if parts.scheme in {"http", "https"} and not parts.netloc.endswith("duckduckgo.com"):
            return href

        return None


class SearXNGProvider:
    """A self-hosted SearXNG, asked for JSON.

    This is the rung that survives, and the reason is maintenance rather than
    cleverness. Every engine-specific scraper we write is a thing that breaks
    when that engine changes its markup or tightens its limits — and they do:
    measured on 4 September 2026, Brave answered 23 of 24 queries directly and
    was returning HTTP 429 to the same scraper an hour later. At that moment a
    local SearXNG answered the identical query with 38 results, 18 of them from
    Brave. It spreads one query across many engines at once, so no single engine
    carries the whole load, and its parsers are kept current by people for whom
    that is the entire job.

    JSON has to be enabled in its settings (`search.formats: [html, json]`); the
    public instances almost never do, which is why this expects a URL you run.
    """

    name = "searxng"
    paid = False
    # SearXNG's own query surface: language, safesearch, time_range, pageno.
    # It has no device switch and no free-text place, so it is dropped from the
    # ladder when either is asked for.
    supports = frozenset({"country", "language", "freshness", "safe_search", "page"})

    TIME_RANGE = {"day": "day", "week": "week", "month": "month", "year": "year"}
    SAFE = {"off": "0", "moderate": "1", "strict": "2"}

    def honours(self, q: SearchQuery) -> bool:
        """`supports` is per-parameter; this is the per-VALUE exception.

        SearXNG's time_range starts at a day, so "results from the last hour"
        is a request it cannot serve even though it does freshness generally.
        Declaring freshness and then quietly widening the window to a day is
        exactly the silent substitution the ladder exists to prevent.
        """
        return q.freshness != "hour"

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")
        self.last_related: list[str] = []

    async def search(self, q: SearchQuery) -> list[SearchResult]:
        import httpx

        limit = q.limit
        params = {"q": q.query, "format": "json"}
        params["safesearch"] = self.SAFE.get(q.safe_search or "off", "0")
        if q.language:
            params["language"] = q.language
        elif q.country:
            params["language"] = f"en-{q.country.upper()}"
        if q.freshness and q.freshness in self.TIME_RANGE:
            params["time_range"] = self.TIME_RANGE[q.freshness]
        if q.page > 1:
            params["pageno"] = str(q.page)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(f"{self._base}/search", params=params)
        except Exception as exc:
            raise SearchUnavailable("Could not reach the search provider.") from exc
        if response.status_code != 200:
            raise SearchUnavailable(f"The search provider returned HTTP {response.status_code}.")

        try:
            payload = response.json()
        except ValueError as exc:
            # A SearXNG without JSON in `search.formats` answers 200 with HTML.
            raise SearchUnavailable("The search provider did not return JSON.") from exc

        # An outage here answers 200 with an empty list, which is exactly what
        # "there is no such thing" looks like — so the ladder fell through to a
        # weaker provider and nobody could tell. SearXNG hands us the evidence:
        # `unresponsive_engines` names every upstream that failed. Zero results
        # WITH failed engines is a broken rung; zero results with healthy
        # engines is an honest empty web, and only the first should fall
        # through. Measured 7 Sep 2026: an expired proxy password left all six
        # engines erroring, the ladder silently switched to DuckDuckGo, and the
        # three most authoritative sources vanished from every result set.
        unresponsive = payload.get("unresponsive_engines") or []
        if not payload.get("results") and unresponsive:
            names = ", ".join(str(u[0]) if isinstance(u, list) else str(u) for u in unresponsive)
            raise SearchUnavailable(
                f"The search provider returned nothing and its engines failed: {names}."
            )

        self.last_related = [str(x) for x in (payload.get("suggestions") or [])][:12]
        out: list[SearchResult] = []
        for row in payload.get("results", []):
            url = (row.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            out.append(
                SearchResult(
                    url=url,
                    title=(row.get("title") or None),
                    description=(row.get("content") or None),
                    position=len(out) + 1,
                )
            )
            if len(out) >= limit:
                break
        return out


def get_provider() -> SerpProvider:
    """Resolve the configured vendor. The default needs no key and no money."""
    name = (settings.search_provider or "").strip().lower()
    if name == "serper" and settings.search_api_key:
        return SerperProvider(settings.search_api_key)
    if name == "none":
        return NullProvider()
    if name and name not in {"duckduckgo", "ddg"}:
        logger.warning("unknown_search_provider", provider=name)
    # No vendor, no key, still a working search: the default costs nothing to run.
    return DuckDuckGoProvider()


class ScrapingdogProvider:
    """Google results, bought rather than scraped.

    Google is the one target on the web that can be bought outright, and buying
    beats building on both counts we measured on 4 September 2026. It refused us
    from six proxy countries AND from no proxy at all, serving its "unusual
    traffic" interstitial in every case — so the objection is not the exit IP,
    and a more expensive exit does not obviously fix it. And at ~1 MB a SERP, a
    thousand Google searches over mobile proxies costs $5.22–$6.83 against
    $0.56–$2.00 to buy the same thousand outright.

    So this rung exists and the mobile one does not. Failed requests are not
    charged, which is what makes it safe to sit in a ladder that may call it
    only when the free rungs have already given up.

    Contract: GET https://api.scrapingdog.com/google, `organic_results[]` with
    `link`, `title`, `snippet`.
    """

    name = "scrapingdog"
    paid = True
    endpoint = "https://api.scrapingdog.com/google"
    # The widest surface of the three, which is most of what it is for: this is
    # the only rung that can answer "what does a phone in Austin see".
    supports = frozenset(
        {
            "country",
            "place",
            "language",
            "device",
            "freshness",
            "safe_search",
            "page",
            "auto_correct",
        }
    )

    TBS = {"hour": "qdr:h", "day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}
    SAFE = {"off": "off", "moderate": "active", "strict": "active"}

    def __init__(self, api_key: str) -> None:
        self._key = api_key
        self.last_related: list[str] = []

    async def search(self, q: SearchQuery) -> list[SearchResult]:
        import httpx

        limit = q.limit
        params = {
            "api_key": self._key,
            "query": q.query,
            "results": str(max(1, min(limit, 100))),
            "country": (q.country or "us").lower(),
        }
        if q.place:
            params["location"] = q.place
        if q.language:
            params["language"] = q.language
        if q.device:
            params["device"] = q.device
        if q.freshness:
            params["tbs"] = self.TBS[q.freshness]
        if q.safe_search:
            params["safe"] = self.SAFE[q.safe_search]
        if q.page > 1:
            params["page"] = str(q.page - 1)  # theirs is 0-based
        if not q.auto_correct:
            params["nfpr"] = "1"
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.get(self.endpoint, params=params)
        except Exception as exc:
            raise SearchUnavailable("Could not reach the search provider.") from exc
        if response.status_code != 200:
            raise SearchUnavailable(f"The search provider returned HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SearchUnavailable("The search provider did not return JSON.") from exc

        # Verified against a real response: rows are {"query": ..., "link": ...}.
        self.last_related = [
            str(x.get("query"))
            for x in (payload.get("relatedSearches") or [])
            if isinstance(x, dict) and x.get("query")
        ][:12]
        rows = payload.get("organic_results")
        if not isinstance(rows, list):
            # A shape change is a provider failure, not an empty web. Falling
            # through to the next rung is right; reporting "no results" is not.
            raise SearchUnavailable("The search provider returned no organic_results.")

        out: list[SearchResult] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = (row.get("link") or "").strip()
            if not url.startswith("http"):
                continue
            # Their `rank` is Google's own position. Counting rows instead
            # would restart at 1 on page 2 and quietly misreport where a
            # result actually sits — the whole point of asking for page 2.
            rank = row.get("rank")
            out.append(
                SearchResult(
                    url=url,
                    title=(row.get("title") or None),
                    description=(row.get("snippet") or None),
                    position=int(rank) if isinstance(rank, int) else len(out) + 1,
                )
            )
            if len(out) >= limit:
                break
        return out


# --- The ladder ---------------------------------------------------------
#
# One provider is one point of failure, and search providers fail often: they
# rate-limit, they change markup, they start refusing an exit. Measured in one
# afternoon — DuckDuckGo went from answering every query to refusing 24 of 24,
# Mojeek from answering to blocking outright, Brave from 23/24 to HTTP 429.
# None of that is unusual; it is what free search does under real volume.
#
# So the endpoint walks a ladder and takes the first rung that answers. A rung
# that fails repeatedly is skipped for a while rather than tried first every
# time, because a provider that just refused us will almost certainly refuse the
# next request too, and the caller pays for that guess in latency.


@dataclass
class _Breaker:
    """Consecutive failures, and when the rung may be tried again."""

    failures: int = 0
    open_until: float = 0.0


_breakers: dict[str, _Breaker] = {}


def _breaker(name: str) -> _Breaker:
    return _breakers.setdefault(name, _Breaker())


def _note_failure(name: str) -> None:
    import time

    b = _breaker(name)
    b.failures += 1
    if b.failures >= max(1, settings.search_breaker_failures):
        b.open_until = time.monotonic() + max(1, settings.search_breaker_seconds)
        logger.warning("search_provider_tripped", provider=name, failures=b.failures)


def _note_success(name: str) -> None:
    _breakers[name] = _Breaker()


def _available(name: str) -> bool:
    import time

    return time.monotonic() >= _breaker(name).open_until


def health_snapshot() -> dict[str, dict[str, float | int | bool]]:
    """Which search rungs are tripped, and for how long.

    The ladder falling through is correct behaviour and was already logged —
    but the CONSEQUENCE is invisible: on 7 Sep 2026 SearXNG went down, the
    ladder switched to DuckDuckGo, and the three most authoritative sources
    silently vanished from every result set for hours. The run got faster,
    which read as an improvement. A degraded ladder has to be inspectable
    without reading logs.
    """
    import time

    now = time.monotonic()
    return {
        name: {
            "consecutiveFailures": b.failures,
            "tripped": now < b.open_until,
            "secondsRemaining": max(0, int(b.open_until - now)),
        }
        for name, b in _breakers.items()
    }


def build_provider(name: str) -> SerpProvider | None:
    """One rung by name, or None when it is not configured on this deployment."""
    name = name.strip().lower()
    if name in {"duckduckgo", "ddg"}:
        return DuckDuckGoProvider()
    if name == "searxng":
        return SearXNGProvider(settings.searxng_url) if settings.searxng_url else None
    if name == "scrapingdog":
        return ScrapingdogProvider(settings.scrapingdog_key) if settings.scrapingdog_key else None
    if name == "serper":
        return SerperProvider(settings.search_api_key) if settings.search_api_key else None
    if name == "none":
        return NullProvider()
    logger.warning("unknown_search_provider", provider=name)
    return None


def ladder() -> list[SerpProvider]:
    """The configured rungs that exist, in preference order.

    `search_provider` still wins when it is set: an operator who named one
    vendor gets that vendor, and nothing silently falls through to another.
    """
    pinned = (settings.search_provider or "").strip().lower()
    if pinned:
        one = build_provider(pinned)
        return [one] if one is not None else [DuckDuckGoProvider()]

    rungs: list[SerpProvider] = []
    for name in (settings.search_ladder or "").split(","):
        if not name.strip():
            continue
        built = build_provider(name)
        if built is not None:
            rungs.append(built)
    return rungs or [DuckDuckGoProvider()]


def _can_honour(provider: SerpProvider, q: SearchQuery) -> bool:
    """Whether this rung can serve everything the caller asked for.

    Two gates: the per-parameter promise in `supports`, and an optional
    per-value `honours()` for the cases where a provider does a parameter but
    not every value of it.
    """
    if not q.demands() <= getattr(provider, "supports", frozenset()):
        return False
    check = getattr(provider, "honours", None)
    return bool(check(q)) if check is not None else True


async def search(q: SearchQuery) -> SearchResponse:
    """Walk the ladder; the first rung with results answers.

    An empty list is not treated as an answer while other rungs remain. A real
    zero-result query is rare and a blocked engine returning a page we parse to
    nothing is common, so the cheap move is to ask the next one. If every rung
    came back empty, that IS the answer and it is returned as empty — an agent
    told "unavailable" retries, an agent told "no results" moves on, and only
    one of those is right when the web genuinely has nothing.
    """
    rungs = ladder()
    tried: list[str] = []
    last_error: Exception | None = None
    any_answered = False

    # A rung that cannot honour what was asked is not a fallback, it is a
    # different answer wearing the same label. Drop it before the walk.
    capable = [p for p in rungs if _can_honour(p, q)]
    if not capable:
        wanted = ", ".join(sorted(q.demands())) or "this query"
        raise SearchUnavailable(
            f"No configured provider can honour {wanted}"
            + (f" (have {', '.join(p.name for p in rungs)})" if rungs else "")
            + "."
        )
    if len(capable) < len(rungs):
        logger.info(
            "search_rungs_excluded",
            demands=sorted(q.demands()),
            kept=[p.name for p in capable],
            dropped=[p.name for p in rungs if p not in capable],
        )

    free_failed: list[str] = []
    for provider in capable:
        if not _available(provider.name):
            logger.info("search_provider_skipped", provider=provider.name)
            if not getattr(provider, "paid", False):
                free_failed.append(provider.name)
            continue
        tried.append(provider.name)
        try:
            found = await provider.search(q)
        except SearchUnavailable as exc:
            last_error = exc
            _note_failure(provider.name)
            if not getattr(provider, "paid", False):
                free_failed.append(provider.name)
            logger.warning("search_provider_failed", provider=provider.name, error=str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - one bad rung must not end the walk
            last_error = exc
            _note_failure(provider.name)
            logger.warning("search_provider_errored", provider=provider.name, error=str(exc))
            continue

        _note_success(provider.name)
        any_answered = True
        if found:
            if getattr(provider, "paid", False) and free_failed and not q.demands():
                # The expensive kind of quiet failure. Nothing is broken from
                # outside — search still answers — but a query the free rungs
                # should have served is now being bought, at five times the
                # credits, on every request until someone notices. Losing the
                # free rung is a COST incident, not an availability one, and it
                # will not show up as an outage anywhere.
                logger.warning(
                    "search_fell_back_to_paid",
                    paid_provider=provider.name,
                    free_rungs_down=free_failed,
                )
            return SearchResponse(
                results=found,
                provider=provider.name,
                paid=bool(getattr(provider, "paid", False)),
                related=list(getattr(provider, "last_related", []) or []),
            )

    if any_answered:
        # Every rung that worked agreed there is nothing here.
        return SearchResponse(results=[], provider=tried[-1] if tried else "none")

    raise SearchUnavailable(
        "No search provider could answer"
        + (f" (tried {', '.join(tried)})" if tried else " (every provider is in backoff)")
        + (f": {last_error}" if last_error else ".")
    )


def cache_key(query: str, limit: int, country: str | None) -> bytes:
    """Cached in `pages` like any other fetch — one store, no divergence."""
    raw = f"serp:{query}|{limit}|{country or ''}".encode()
    return hashlib.sha256(raw).digest()
