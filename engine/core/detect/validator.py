"""Block and soft-failure detection — principle P2: never trust HTTP 200.

Modern anti-bot systems rarely return 403. They return 200 with a challenge
page, a consent wall, a login gate, or generated decoy content. Content from
here flows into an LLM pipeline and a database, so a scraper that trusts status
codes will happily store a generated decoy and nobody notices until something
downstream cites it.

Called twice per fetch:
  1. post-fetch, pre-extraction — cheap checks on status, headers, raw body
  2. post-extraction — checks needing extracted text and the confidence score

Splitting them avoids paying for extraction on an obvious challenge page.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog
import yaml

from engine.core.detect.plausibility import assess as assess_plausibility
from engine.core.fetch.base import FetchResult

logger = structlog.get_logger(__name__)


def _signature_path() -> Path | None:
    """Where the body signatures live, or None when they are not installed.

    The signatures are proprietary (engine/knowledge): which strings identify
    which WAF is learned from every blocked page we have fetched. The open core
    runs without them — layer 2 simply has nothing to match, and layers 1, 3
    and 4 carry block detection on their own.
    """
    try:
        from engine.knowledge import SIGNATURE_PATH
    except ImportError:
        return None
    return SIGNATURE_PATH if SIGNATURE_PATH.is_file() else None


# Below this many recorded successes a domain has no usable baseline and the
# statistical layer must not fire (cold start).
MIN_SUCCESSES_FOR_STATS = 20

# Absolute fallbacks used during cold start.
COLD_START_MIN_WORDS = 50
COLD_START_MIN_CONFIDENCE = 0.2

# Below this many extracted words a 403 is a refusal, however large the
# document around it. etsy.com's real 403 results pages run to thousands.
FORBIDDEN_PAGE_MIN_WORDS = 200


class Reason:
    BLOCKED = "BLOCKED"
    TARGET_ERROR = "TARGET_ERROR"
    ROBOTS_DENIED = "ROBOTS_DENIED"
    SOFT_BLOCK = "SOFT_BLOCK"
    EMPTY = "EMPTY"
    # The fetch worked and the page is not a challenge — but nothing usable came
    # back: an empty body on a 2xx, or a navigation shell with no content in it.
    # Distinct from SOFT_BLOCK on purpose: a soft block raises the domain's tier
    # floor for a week, and a thin page must never do that. It climbs one tier
    # like a soft block does; it just leaves no mark behind.
    THIN = "THIN"


@dataclass
class Verdict:
    ok: bool
    reason: str | None = None
    signal: str | None = None
    confidence: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    vendor: str | None = None

    @classmethod
    def good(cls, details: dict[str, Any] | None = None) -> Verdict:
        return cls(ok=True, details=details or {})


@dataclass
class Signature:
    id: str
    vendor: str
    any_of: tuple[str, ...] = ()
    all_of: tuple[str, ...] = ()
    confidence: float = 0.9
    requires_low_content: bool = False

    def matches(self, haystack: str) -> bool:
        if self.all_of and not all(needle in haystack for needle in self.all_of):
            return False
        if self.any_of:
            return any(needle in haystack for needle in self.any_of)
        return bool(self.all_of)


@dataclass
class DomainStats:
    """The slice of domain_profiles the detector needs."""

    domain: str = ""
    success_count: int = 0
    avg_content_length: int | None = None
    stdev_content_length: int | None = None

    @property
    def has_baseline(self) -> bool:
        return (
            self.success_count >= MIN_SUCCESSES_FOR_STATS
            and self.avg_content_length is not None
            and self.stdev_content_length is not None
            and self.stdev_content_length > 0
        )


@dataclass
class ExtractionSummary:
    """The slice of an extraction result the detector needs."""

    word_count: int = 0
    char_count: int = 0
    confidence: float = 0.0
    link_count: int = 0
    title: str | None = None
    # Layer 4 needs the page's incidental markers: a generated decoy tends to
    # lack the ordinary furniture of a real page.
    markdown: str = ""
    page_type: str = "unknown"
    external_link_count: int = 0
    # "fallback" means the router could not recognise the page and converted
    # the main node wholesale — on its own a reason to look harder.
    extraction_path: str = ""
    has_images: bool = False
    has_author: bool = False


@lru_cache(maxsize=1)
def _load_signatures() -> tuple[tuple[Signature, ...], frozenset[str], tuple[str, ...]]:
    path = _signature_path()
    if path is None:
        # Say so once, loudly. A silently signature-less validator would look
        # like a validator that simply never sees a block.
        logger.warning(
            "block_signatures_unavailable",
            detail="engine/knowledge is not installed; body-signature detection is off",
        )
        return (), frozenset(), ()
    raw = yaml.safe_load(path.read_text()) or {}
    signatures = tuple(
        Signature(
            id=str(item["id"]),
            vendor=str(item.get("vendor", "generic")),
            any_of=tuple(str(s).lower() for s in item.get("any", [])),
            all_of=tuple(str(s).lower() for s in item.get("all", [])),
            confidence=float(item.get("confidence", 0.9)),
            requires_low_content=bool(item.get("requires_low_content", False)),
        )
        for item in raw.get("signatures", [])
    )
    titles = frozenset(str(t).lower() for t in raw.get("challenge_titles", []))
    hosts = tuple(str(h).lower() for h in raw.get("challenge_hosts", []))
    return signatures, titles, hosts


def reload_signatures() -> None:
    """Drop the cache so an edited signatures.yaml takes effect without a deploy."""
    _load_signatures.cache_clear()


# --------------------------------------------------------------------------
# Layer 1 — status and headers
# --------------------------------------------------------------------------

# Status codes that mean "the page is not there", never "we were blocked".
# Escalating on these sends a request to tier 3 to fetch a page that does not
# exist — a hundred times the cost for the same nothing.
NON_BLOCK_STATUSES = frozenset({400, 401, 404, 405, 410, 451})

# Content types we can extract from. Anything else is unsupported content, not
# protection: a JPEG that fetches perfectly well yields no words, and treating
# that as a soft block raises the domain's tier and poisons the profile for
# every later page on the site.
# ---------------------------------------------------------------------------
# CLOSED sets are enumerated. OPEN sets are judged by their content.
#
# A status code is closed: the RFC defines them, nobody invents one, so
# NON_BLOCK_STATUSES and TRANSIENT_STATUSES are lists and that is correct.
#
# A content type is OPEN. Any server may serve anything under any label, so a
# whitelist is a standing guess about other people's software — and it was
# wrong four times before this was written down: RSS/Atom feeds, vendor
# `+json` types, Apple answering JSON as `text/javascript`, and a competitor's
# agent-onboarding file served as `text/markdown`. Each was fixed by adding an
# entry, which is fixing the instance and leaving the class.
#
# So the lists below are a FAST PATH, not the decision. `is_extractable`
# accepts any `text/*` and anything whose body reads as data; the list only
# spares the sniff. A new label costs nothing now.
# ---------------------------------------------------------------------------

EXTRACTABLE_CONTENT_TYPES = (
    "text/html",
    "application/xhtml",
    "text/plain",
    "application/xml",
    "text/xml",
    "application/json",
    "application/pdf",
)


# A machine-readable body: correct when it has no prose, no links and no site
# furniture. Every content check below this line assumes an HTML PAGE, and on a
# JSON or XML body those assumptions invert — "few words, no links" is what a
# healthy API response looks like.
STRUCTURED_CONTENT_TYPES = (
    "application/json",
    "application/xml",
    "text/xml",
    "application/ld+json",
    "application/rss+xml",
    "application/atom+xml",
    # JSON served under its JSONP-era labels. Apple's review feeds answer
    # `text/javascript` with a JSON body, and refusing them cost a harvest its
    # entire quantitative base until it worked around us via /v1/fetch
    # (reported 8 Sep 2026).
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    # Agent-facing docs are served as markdown: llms.txt, SKILL.md, README.md.
    "text/markdown",
)


def looks_like_structured(body: bytes | None) -> bool:
    """Is this body DATA rather than a page, whatever the server called it?

    JSON or XML by inspection. Structured data skips the extraction-confidence
    floor, so getting this wrong meant a JSON API scoring SOFT_BLOCK and
    raising the tier for a whole host.
    """
    return looks_like_json(body) or looks_like_xml(body)


def looks_like_xml(body: bytes | None) -> bool:
    if not body:
        return False
    head = body[:512].lstrip()
    return head.startswith(b"<?xml") or head.startswith(b"<rss") or head.startswith(b"<feed")


def looks_like_json(body: bytes | None) -> bool:
    """Is this body JSON, whatever the server called it?

    The header list is a whitelist and every whitelist is a guess about other
    people's servers. This has been wrong twice: once for feeds and vendor
    `+json` types, once for Apple answering `text/javascript`. Judging the BODY
    catches the next one without needing to know its label in advance.

    Deliberately cheap — the first non-space character and a bounded parse, not
    a full decode of a large document.
    """
    if not body:
        return False
    head = body[:2048].lstrip()
    if head[:1] not in (b"{", b"["):
        return False
    try:
        json.loads(body[:1_000_000].decode("utf-8", "ignore"))
    except (ValueError, UnicodeDecodeError):
        return False
    return True


def is_structured_data(content_type: str | None) -> bool:
    """True for a body that is data rather than a page.

    Without this a 200 from a JSON API scored SOFT_BLOCK on the extraction
    confidence floor, and the verdict was written to the domain profile: one API
    URL raised the tier and opened the circuit for an entire host, so nothing on
    it could be fetched afterwards. Found doing keyless GitHub API research with
    our own engine (6 Sep 2026). Sitemaps, feeds and llms.txt are all in this
    class too, and "ask the platform for its data" runs straight through it.
    """
    if not content_type:
        return False
    base = content_type.split(";", 1)[0].strip().lower()
    return any(base.startswith(prefix) for prefix in STRUCTURED_CONTENT_TYPES) or base.endswith(
        "+json"
    )


def is_plain_text(content_type: str | None) -> bool:
    """True for `text/plain`: a file, not a page."""
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == "text/plain"


def _looks_like_html(body: bytes) -> bool:
    head = body[:1024].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or b"<html" in head


def is_extractable(content_type: str | None) -> bool:
    """True when the body is something the extraction layer can read.

    A missing content type is treated as extractable: plenty of servers omit
    it, and refusing those would lose real pages.
    """
    if not content_type:
        return True
    base = content_type.split(";", 1)[0].strip().lower()
    # Anything we call structured data is by definition readable — deriving it
    # rather than repeating it keeps the two from disagreeing. They did: RSS,
    # Atom and vendor `+json` types were structured but "unsupported", so a feed
    # was refused with TARGET_ERROR before the body was ever looked at, and
    # /v1/posts depends on feeds (6 Sep 2026).
    if is_structured_data(content_type):
        return True
    # ANY text type is readable. The whitelist has been wrong four times now —
    # feeds, vendor `+json`, `text/javascript`, and `text/markdown`, that last
    # one found trying to read a competitor's own agent-onboarding file, which
    # is exactly the kind of document this engine exists to read. Enumerating
    # the readable half of `text/*` was always the wrong way round: the rule
    # exists to stop us escalating to a browser to re-fetch an IMAGE, and no
    # text type is ever that.
    if base.startswith("text/"):
        return True
    return any(base.startswith(prefix) for prefix in EXTRACTABLE_CONTENT_TYPES)


def _layer1(result: FetchResult) -> Verdict | None:
    # The fetcher refused the response itself — too large to carry through a
    # paid exit, or a file no rung can make a page of. A target error, so the
    # ladder stops: read as a transport error it would be retried, then
    # re-downloaded whole by every rung above (see fetch/binary.py).
    if result.refused:
        return Verdict(
            ok=False,
            reason=Reason.TARGET_ERROR,
            signal=result.refused,
            confidence=1.0,
            details={"status_code": result.status_code, "message": result.refused_detail},
        )
    if result.error is not None:
        lowered = result.error.lower()
        reason = Reason.EMPTY if "timeout" in lowered else Reason.EMPTY
        return Verdict(
            ok=False,
            reason=reason,
            signal="transport_error",
            confidence=1.0,
            details={"error": result.error},
        )

    status = result.status_code
    headers = result.headers

    # Header-level WAF markers fire regardless of status.
    if "cf-mitigated" in headers:
        return Verdict(
            ok=False,
            reason=Reason.BLOCKED,
            signal="cloudflare_mitigated",
            confidence=0.98,
            vendor="cloudflare",
        )
    # DataDome. `x-datadome: protected`, the `datadome` cookie AND `x-dd-b` all
    # ride on responses DataDome lets through — measured on thebump (200, real
    # article) and etsy (403 with x-dd-b=259 and a 600KB real results page).
    # Even the status is not the tell: DataDome flags a client and serves the
    # page anyway. Keying on any of those called 8 domains blocked with 46
    # blocks and 0 successes, every one false (7 Sep 2026).
    #
    # The one thing only a challenge has is the challenge: the
    # captcha-delivery loader in the body. A refusal status with a body too
    # small to be a page is the other honest signal.
    if "x-datadome" in headers or "datadome" in headers.get("set-cookie", "").lower():
        body_head = result.body[:20_000].lower() if result.body else b""
        challenged = b"captcha-delivery.com" in body_head or (
            status in (401, 403) and len(result.body or b"") < 20_000
        )
        if challenged:
            return Verdict(
                ok=False,
                reason=Reason.BLOCKED,
                signal="datadome",
                confidence=0.95,
                vendor="datadome",
            )
        # Protected and allowed. Say nothing; let the body speak for itself.
    if "ddos-guard" in headers.get("server", "").lower() and len(result.body) < 2_000:
        return Verdict(
            ok=False,
            reason=Reason.BLOCKED,
            signal="ddos_guard",
            confidence=0.9,
            vendor="ddos-guard",
        )

    # Redirected onto a challenge host.
    _, _, challenge_hosts = _load_signatures()
    final_host = (urlsplit(result.url).hostname or "").lower()
    for host in challenge_hosts:
        if final_host and (final_host == host or final_host.endswith("." + host)):
            return Verdict(
                ok=False,
                reason=Reason.BLOCKED,
                signal="challenge_redirect",
                confidence=0.95,
                details={"host": final_host},
            )

    if status is None:
        return Verdict(ok=False, reason=Reason.EMPTY, signal="no_status", confidence=1.0)

    if status in NON_BLOCK_STATUSES:
        return Verdict(
            ok=False,
            reason=Reason.TARGET_ERROR,
            signal=f"status_{status}",
            confidence=1.0,
            details={"status_code": status},
        )

    # 202 is "accepted, still working" — with no body it is not a page, and
    # babycenter returned exactly that as a success with 0 chars. Reported as a
    # target status so the escalation's transient retry gets one more look
    # (a 202 very often becomes a 200 a second later), then fails honestly.
    if status == 202 and not result.body:
        return Verdict(
            ok=False,
            reason=Reason.TARGET_ERROR,
            signal="status_202",
            confidence=1.0,
            details={"status_code": 202},
        )

    # A body we cannot extract from is unsupported content, never a block.
    # Classifying it as one would escalate to a browser tier to re-fetch an
    # image, and would raise the domain's working tier on the way.
    if (
        status == 200
        and not is_extractable(result.content_type)
        and not looks_like_json(result.body)
    ):
        return Verdict(
            ok=False,
            reason=Reason.TARGET_ERROR,
            signal="unsupported_content_type",
            confidence=1.0,
            details={"content_type": result.content_type},
        )

    # A FILE, whatever the server called it: an archive labelled text/html is
    # still an archive, and no browser rung will render it into a page.
    if 200 <= status < 300:
        from engine.core.fetch.binary import refused_kind

        kind = refused_kind(result.url, result.content_type, result.body)
        if kind is not None:
            return Verdict(
                ok=False,
                reason=Reason.TARGET_ERROR,
                signal="binary_content",
                confidence=1.0,
                details={
                    "status_code": status,
                    "kind": kind,
                    "message": f"The URL is a {kind} file, not a page",
                },
            )

    if status == 403:
        # A 403 with a whole page behind it is the page. etsy.com serves its
        # full 600KB results with a 403 when DataDome has flagged the client —
        # the status is a mark against us, the content is still there. A 403
        # with nothing behind it is what a refusal looks like. Defer the large
        # ones to the body layers, which know a shell from an article.
        if len(result.body or b"") >= 20_000:
            return None
        return Verdict(
            ok=False,
            reason=Reason.BLOCKED,
            signal="status_403",
            confidence=0.9,
            details={"status_code": status},
        )
    if status == 429:
        retry_after = result.headers.get("retry-after")
        return Verdict(
            ok=False,
            reason=Reason.BLOCKED,
            signal="status_429",
            confidence=0.95,
            details={"status_code": status, "retry_after": retry_after},
        )
    if status in (503, 502, 520, 521, 522, 525):
        # A 5xx is only a block when challenge markers are present; the body
        # check in layer 2 decides. Returning None defers to it.
        return None
    if status >= 500:
        return None
    if status >= 400:
        return Verdict(
            ok=False,
            reason=Reason.TARGET_ERROR,
            signal=f"status_{status}",
            confidence=0.9,
            details={"status_code": status},
        )
    return None


# --------------------------------------------------------------------------
# Layer 2 — body signatures
# --------------------------------------------------------------------------

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Signature matching reads a bounded slice — scanning a 5MB body for every
# signature is wasted work — but it reads the HEAD **and the TAIL**. Injected
# anti-bot scripts sit just before </body>, so a head-only window makes
# detection depend on page length: the same marker is seen on a short page and
# missed on a long one. That is luck, not a threshold.
_SCAN_HEAD_BYTES = 150_000
_SCAN_TAIL_BYTES = 50_000


# What counts as "nothing on the page". The same number either side of
# extraction, so the first pass and the second agree about a given page.
LOW_CONTENT_WORDS = 200
_TAGS = re.compile(r"<[^>]+>")
_SCRIPTS = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)


def _visible_words(html: str) -> int:
    """Roughly what a reader would see, for judging an un-extracted page."""
    return len(_TAGS.sub(" ", _SCRIPTS.sub(" ", html)).split())


def _scan_window(result: FetchResult) -> str:
    body = result.body
    if len(body) <= _SCAN_HEAD_BYTES + _SCAN_TAIL_BYTES:
        return result.text().lower()

    head = FetchResult(
        url=result.url,
        status_code=result.status_code,
        headers=result.headers,
        body=body[:_SCAN_HEAD_BYTES],
        content_type=result.content_type,
        tier=result.tier,
        latency_ms=0,
        bytes_transferred=0,
    ).text()
    tail = FetchResult(
        url=result.url,
        status_code=result.status_code,
        headers=result.headers,
        body=body[-_SCAN_TAIL_BYTES:],
        content_type=result.content_type,
        tier=result.tier,
        latency_ms=0,
        bytes_transferred=0,
    ).text()
    return (head + "\n" + tail).lower()


# --------------------------------------------------------------------------
# Site furniture — the difference between "little prose" and "withheld"
# --------------------------------------------------------------------------

# A challenge shell is stripped: no navigation, no search box, a handful of
# links. A real page keeps its furniture however little prose it extracts to.
# dictionary.com's homepage extracts to 31 words and was reported BLOCKED at
# every tier (measured, Sep 2026) because 31 < 200 satisfied
# `requires_low_content` and the page happens to load reCAPTCHA for its own
# search form. Word count alone cannot tell a listing page from a wall.
_SEARCH_INPUT_RE = re.compile(
    r"""<input\b[^>]*\btype\s*=\s*['"]?(?:text|search|email)\b|"""
    r"""<input\b(?![^>]*\btype\s*=)[^>]*>|"""
    r"""role\s*=\s*['"]search['"]|"""
    r"""<textarea\b""",
    re.IGNORECASE,
)
_NAV_RE = re.compile(r"""<nav\b|role\s*=\s*['"]navigation['"]""", re.IGNORECASE)
_ANCHOR_RE = re.compile(r"<a\b[^>]*\bhref\s*=", re.IGNORECASE)

# A challenge page can carry one form (the turnstile) and a couple of links.
FURNITURE_MIN_LINKS = 10


@dataclass(frozen=True)
class Furniture:
    """The ordinary structural parts of a working page."""

    has_search_input: bool = False
    has_nav: bool = False
    link_count: int = 0

    @property
    def present(self) -> bool:
        """A page a person could actually use: somewhere to type, or a
        navigation landmark, AND enough links to be part of a site."""
        return (self.has_search_input or self.has_nav) and self.link_count >= FURNITURE_MIN_LINKS

    def as_details(self) -> dict[str, Any]:
        return {
            "search_input": self.has_search_input,
            "nav": self.has_nav,
            "links": self.link_count,
        }


def site_furniture(html: str) -> Furniture:
    """Read the page's furniture off the raw markup.

    Raw markup, not extracted text, because extraction is exactly what fails on
    the pages this has to judge. Regex rather than a parse: this runs on every
    fetch, before we have decided the body is worth parsing.
    """
    return Furniture(
        has_search_input=_SEARCH_INPUT_RE.search(html) is not None,
        has_nav=_NAV_RE.search(html) is not None,
        link_count=len(_ANCHOR_RE.findall(html)),
    )


def extract_title(html: str) -> str | None:
    match = _TITLE_RE.search(html)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip() or None


def _layer2(
    result: FetchResult,
    extraction: ExtractionSummary | None,
    furniture: Furniture | None = None,
) -> tuple[tuple[Signature, float] | None, list[str]]:
    """Returns the strongest qualifying signature, plus the ids of signatures
    that matched but were suppressed by `requires_low_content`.

    The suppressed list is near-miss data: it is exactly what tells us later
    whether a threshold is set correctly, and it cannot be recovered without
    re-crawling if we throw it away here.
    """
    signatures, _, _ = _load_signatures()
    body = _scan_window(result)
    # `requires_low_content` asks "is the content absent?" and answers it with
    # a word count. Prose is not the only kind of content: a page carrying a
    # search box, navigation and eighty links has not been withheld, whatever
    # trafilatura made of it.
    has_furniture = furniture is not None and furniture.present
    # Before extraction there IS no word count, and reading that absence as
    # "the content is absent" condemned every page carrying one of these
    # markers: a 924 KB home page with 1,429 words of real content, fetched at
    # 200 by every rung, was refused as a Cloudflare block before anything
    # looked at it (measured, Sep 2026). The furniture scan could not save it
    # either — that reads the first 150 KB and last 50 KB, and on a page that
    # size the navigation sits in neither. So on the first pass, ask the PAGE.
    low_content = not has_furniture and (
        extraction.word_count < LOW_CONTENT_WORDS
        if extraction is not None
        else _visible_words(body) < LOW_CONTENT_WORDS
    )

    best: tuple[Signature, float] | None = None
    suppressed: list[str] = []
    for sig in signatures:
        if not sig.matches(body):
            continue
        if sig.requires_low_content and not low_content:
            suppressed.append(sig.id)
            continue
        if best is None or sig.confidence > best[1]:
            best = (sig, sig.confidence)
    return best, suppressed


_SOFT_404_RE = re.compile(
    r"\b(page not found|not found|404|doesn.t exist|does not exist|no longer available"
    r"|page unavailable|nothing here|oops)\b",
    re.IGNORECASE,
)
SOFT_404_MAX_WORDS = 150


def _is_soft_404(title: str | None, word_count: int) -> bool:
    """A 'not found' title on a thin page. The word bound keeps a real article
    about HTTP 404s, or a shop's 'Page Not Found' help centre entry, as content."""
    if not title or word_count > SOFT_404_MAX_WORDS:
        return False
    return _SOFT_404_RE.search(title) is not None


def _title_is_challenge(title: str | None) -> bool:
    if not title:
        return False
    _, challenge_titles, _ = _load_signatures()
    lowered = title.lower().strip()
    return any(t in lowered for t in challenge_titles)


# --------------------------------------------------------------------------
# Layer 3 — statistical soft-block
# --------------------------------------------------------------------------


def _layer3(
    result: FetchResult,
    stats: DomainStats,
    extraction: ExtractionSummary,
    furniture: Furniture | None = None,
) -> Verdict | None:
    raw_len = len(result.body)
    # Both low-prose signals below ask the same question — "was the content
    # withheld?" — and a homepage or index answers "no, it is simply an index".
    # Furniture is what separates the two; without it these fire on every
    # portal page on the web (measured, Sep 2026).
    has_furniture = furniture is not None and furniture.present

    # Soft 404: a 200 whose title says the page does not exist. Stored as
    # content it becomes a "page" in a crawl and a cached hit for a URL that has
    # nothing behind it. TARGET_ERROR, not a block — escalating buys nothing.
    if _is_soft_404(extraction.title, extraction.word_count):
        return Verdict(
            ok=False,
            reason=Reason.TARGET_ERROR,
            signal="soft_404",
            confidence=0.85,
            details={"title": extraction.title, "word_count": extraction.word_count},
        )

    # Near-empty: substantial HTML, almost no extracted text. Content was not
    # rendered or is gated.
    if extraction.word_count < COLD_START_MIN_WORDS and raw_len > 10_000 and not has_furniture:
        return Verdict(
            ok=False,
            # THIN, not SOFT_BLOCK. Its own comment says "content was not
            # rendered or is gated" — that is a judgement about the PAGE, and a
            # soft block calls apply_block, which raises the domain's tier floor
            # for a week. Every JS app and every login-gated page we touched was
            # taxing that domain to the 5-credit rung for ever after. THIN still
            # climbs a rung; it just leaves no mark on the domain.
            reason=Reason.THIN,
            signal="near_empty",
            confidence=0.75,
            details={"word_count": extraction.word_count, "raw_bytes": raw_len},
        )

    # Link-only: a nav page served instead of content.
    if extraction.link_count >= 20 and extraction.word_count < 30 and not has_furniture:
        return Verdict(
            ok=False,
            # Also THIN: "a nav page served instead of content" is the nav shell
            # by another name, and nav_shell is already THIN.
            reason=Reason.THIN,
            signal="link_only",
            confidence=0.7,
            details={"links": extraction.link_count, "words": extraction.word_count},
        )

    if _title_is_challenge(extraction.title):
        return Verdict(
            ok=False,
            reason=Reason.SOFT_BLOCK,
            signal="challenge_title",
            confidence=0.85,
            details={"title": extraction.title},
        )

    if extraction.confidence < COLD_START_MIN_CONFIDENCE and result.status_code == 200:
        return Verdict(
            ok=False,
            reason=Reason.SOFT_BLOCK,
            signal="confidence_floor",
            confidence=0.7,
            details={"extraction_confidence": extraction.confidence},
        )

    # Statistical check proper — only with a real baseline (cold start guard).
    if stats.has_baseline:
        assert stats.avg_content_length is not None
        assert stats.stdev_content_length is not None
        deviation = (stats.avg_content_length - extraction.char_count) / stats.stdev_content_length
        if deviation > 3.0:
            # Confidence scales with how far below baseline we landed, capped.
            confidence = min(0.95, 0.6 + (deviation - 3.0) * 0.1)
            return Verdict(
                ok=False,
                reason=Reason.SOFT_BLOCK,
                signal="below_baseline",
                confidence=confidence,
                details={
                    "chars": extraction.char_count,
                    "baseline": stats.avg_content_length,
                    "deviations_below": round(deviation, 2),
                },
            )
    return None


def uniform_length_suspicion(lengths: list[int], tolerance: float = 0.03) -> bool:
    """Decoy detector: many distinct URLs returning near-identical lengths.

    When a crawl of 50 distinct URLs returns 50 bodies within a few percent of
    the same length, we are being served a template, not content. This is the
    strongest cheap signal available against generated decoy content.
    """
    usable = [n for n in lengths if n > 0]
    if len(usable) < 5:
        return False
    mean = sum(usable) / len(usable)
    if mean <= 0:
        return False
    variance = sum((n - mean) ** 2 for n in usable) / len(usable)
    return (math.sqrt(variance) / mean) < tolerance


# --------------------------------------------------------------------------
# Combination
# --------------------------------------------------------------------------


# Elements whose presence IS the content: a game in an iframe, a video, a
# canvas app. A page built around one of these extracts to little or no prose
# and must not be read as an empty block.
_CONTENT_EMBED = re.compile(rb"<(?:iframe|embed|object|video|canvas)\b[^>]*>", re.I)
# An embed that says it is not shown: tracking-pixel sandboxes and sync frames
# are iframes too, and every one on a real retail page was marked like this.
_NOT_SHOWN = re.compile(
    rb'aria-hidden\s*=\s*["\']?true|\shidden[\s>=]|display\s*:\s*none|visibility\s*:\s*hidden',
    re.I,
)
# The exemption was written for a page whose body IS one embed — a 6 KB game
# page. A page with this much text of its own is not that page, whatever else
# it embeds, and an empty extraction of it is a failed extraction.
EMBED_PAGE_MAX_TEXT = 3_000
_SCRIPT_OR_STYLE = re.compile(rb"<(script|style|noscript)\b.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(rb"<[^>]+>")
# Hosts that serve a challenge widget in an iframe rather than content. An embed
# pointing at one of these is the block, not the exception to it.
_CAPTCHA_HOSTS = (
    b"challenges.cloudflare.com",
    b"captcha-delivery.com",
    b"hcaptcha.com",
    b"recaptcha",
    b"turnstile",
    b"arkoselabs",
    b"funcaptcha",
    b"geo.captcha",
)


def _has_content_embed(result: FetchResult) -> bool:
    """A 2xx whose main content is a non-challenge embed.

    Only consulted once the text checks have already decided the page is thin,
    so it never overrides a page that has real prose. The embed's src must not
    be a captcha host, and no known challenge host may appear in the body — a
    turnstile in an iframe is a block, an azgame.io game is not.
    """
    if result.status_code is not None and result.status_code >= 400:
        return False
    body = result.body or b""
    # Only embeds that are actually shown. A retail home page whose extraction
    # had failed was waved through on five aria-hidden tracking iframes and a
    # cart-sync frame (Sep 2026).
    if not any(not _NOT_SHOWN.search(m.group(0)) for m in _CONTENT_EMBED.finditer(body)):
        return False
    # And only when the embed is the page: that one had 97,000 characters of
    # its own text, all of it lost, which no iframe explains.
    text = _TAG.sub(b" ", _SCRIPT_OR_STYLE.sub(b" ", body))
    if len(b"".join(text.split())) > EMBED_PAGE_MAX_TEXT:
        return False
    head = body[:20_000].lower()
    if any(host in head for host in _CAPTCHA_HOSTS):
        return False
    _, _, challenge_hosts = _load_signatures()
    return not any(host.encode() in head for host in challenge_hosts)


def validate(
    result: FetchResult,
    stats: DomainStats | None = None,
    extraction: ExtractionSummary | None = None,
) -> Verdict:
    """Combine every layer into one verdict (05-block-detection.md section 7).

    `extraction` is None on the pre-extraction pass, which restricts the checks
    to status, headers and body signatures.
    """
    stats = stats or DomainStats()

    # 1. Hard status/header signals win outright.
    layer1 = _layer1(result)
    if layer1 is not None and layer1.reason in (Reason.TARGET_ERROR, Reason.EMPTY):
        return layer1
    if layer1 is not None and not layer1.ok:
        return layer1

    # A structured body has already passed the checks that can judge it: status,
    # headers, and a challenge page is never served as application/json. The
    # prose-shaped layers below would read a healthy API response as a soft
    # block, and that verdict poisons the whole domain's profile.
    if is_structured_data(result.content_type) and result.body:
        return Verdict(ok=True, reason=None, signal=None, confidence=1.0, details={})

    # A plain-text file is not a page either. A word list, robots.txt or a log
    # has no prose, no links and no furniture, and the page-shaped layers below
    # judged one `implausible_content` and climbed all six tiers, paying for
    # each (a dictionary word list, 23 Sep 2026). No bot wall serves its
    # challenge as text/plain, so only a body that is really HTML under the
    # label still goes through the layers.
    if (
        is_plain_text(result.content_type)
        and result.body
        and (result.status_code or 200) < 400
        and not _looks_like_html(result.body)
    ):
        return Verdict(ok=True, reason=None, signal=None, confidence=1.0, details={})

    # 2. Body signatures, read against the page's own structure.
    furniture = site_furniture(_scan_window(result))
    signature_hit, suppressed_signatures = _layer2(result, extraction, furniture)
    statistical: Verdict | None = None
    if extraction is not None:
        statistical = _layer3(result, stats, extraction, furniture)

    if signature_hit is not None:
        sig, confidence = signature_hit
        if confidence >= 0.9:
            return Verdict(
                ok=False,
                reason=Reason.BLOCKED,
                signal=sig.id,
                confidence=confidence,
                vendor=sig.vendor,
                details={"signature": sig.id},
            )
        if confidence >= 0.7 and statistical is not None:
            return Verdict(
                ok=False,
                reason=Reason.BLOCKED,
                signal=sig.id,
                confidence=max(confidence, statistical.confidence),
                vendor=sig.vendor,
                details={"signature": sig.id, "statistical": statistical.signal},
            )
        # A signature gated on low content that HAS low content already has its
        # second signal: the gate was the corroboration. Waiting for a
        # statistical baseline as well let Google's "Before you continue"
        # consent page through as a 200 on a cold-start domain (5 Sep 2026) —
        # a page with 1.4KB of legal text and no results, reported as success.
        if (
            confidence >= 0.7
            and sig.requires_low_content
            and extraction is not None
            and extraction.word_count < 200
            and not furniture.present
        ):
            return Verdict(
                ok=False,
                reason=Reason.SOFT_BLOCK,
                signal=sig.id,
                confidence=confidence,
                vendor=sig.vendor,
                details={"signature": sig.id, "word_count": extraction.word_count},
            )

    # 3. Statistical soft block on its own needs high confidence.
    if statistical is not None and statistical.confidence >= 0.7:
        return _corroborate(statistical, signature_hit)

    # 4. Content plausibility. The layer that matters most here: content flows
    # into an LLM pipeline and a database, so a decoy that gets through is a
    # fabricated fact with a real URL attached rather than a failed fetch.
    plausibility = None
    if extraction is not None and extraction.markdown:
        plausibility = assess_plausibility(
            url=result.url,
            markdown=extraction.markdown,
            page_type=extraction.page_type,
            link_count=extraction.link_count,
            external_link_count=extraction.external_link_count,
            has_images=extraction.has_images,
            has_author=extraction.has_author,
        )
        if plausibility.implausible:
            return Verdict(
                ok=False,
                reason=Reason.SOFT_BLOCK,
                signal="implausible_content",
                confidence=plausibility.confidence,
                details=plausibility.as_details(),
            )

    # 4. A 5xx that reached here carried no challenge markers — origin failure.
    if result.status_code is not None and result.status_code >= 500:
        return Verdict(
            ok=False,
            reason=Reason.TARGET_ERROR,
            signal=f"status_{result.status_code}",
            confidence=0.8,
            details={"status_code": result.status_code},
        )

    # 5. Nothing usable came back. Not a block — nothing here writes to the
    #    domain profile — but not a success either. ancestry.com answered four
    #    different names with the same 608 bytes of menu (and sometimes 0), and
    #    every one was stored as content.
    if extraction is not None:
        thin = _thin(extraction)
        if thin is not None:
            # An embed page — an iframe game, a video, a canvas app — extracts
            # to no prose because its content lives in another frame, but it is
            # VALID content, not an empty block. Measured 12 Sep 2026:
            # horrorgames.io/*.embed is a 6 KB 200 whose body is one <iframe>;
            # it was read as empty_content, corroborated to SOFT_BLOCK by a
            # stray low-confidence signature, and escalated through all four
            # tiers to BLOCKED — four fetches, deep-tier credits, for a page the
            # http tier got clean. A high-confidence challenge has already
            # returned above; a challenge iframe (its src on a captcha host) is
            # excluded here, so what remains is genuine embedded content.
            if _has_content_embed(result):
                return Verdict.good({"signal": "embed_content"})
            return _corroborate(thin, signature_hit)

    # 6. A 403 that turned out to carry almost nothing. The status layer hands
    #    a big 403 on in case it is a real page (etsy.com serves whole results
    #    with one), but "big" was measured on the raw document, and a browser
    #    tier's rendered document is always big. indeed.com's 59-word "Request
    #    Blocked" page came back from the browser tier at ~44KB, passed every
    #    body check, and was returned — and billed — as a success, so the
    #    retry from another country that actually got the listings never ran.
    #    A real page behind a 403 has hundreds of words; a refusal has a few.
    if (
        result.status_code == 403
        and extraction is not None
        and extraction.word_count < FORBIDDEN_PAGE_MIN_WORDS
    ):
        return Verdict(
            ok=False,
            reason=Reason.BLOCKED,
            signal="status_403",
            confidence=0.9,
            details={"status_code": 403, "word_count": extraction.word_count, "rendered": True},
        )

    # Near-miss data is recorded even on success so thresholds can be tuned
    # later without re-crawling.
    near_miss: dict[str, Any] = {}
    if signature_hit is not None:
        near_miss["signature_near_miss"] = signature_hit[0].id
    elif suppressed_signatures:
        # Matched the body but was held back by the low-content requirement.
        near_miss["signature_near_miss"] = suppressed_signatures[0]
        near_miss["suppressed_by_content_length"] = suppressed_signatures
    if statistical is not None:
        near_miss["statistical_near_miss"] = statistical.signal
    if plausibility is not None and plausibility.fired:
        # Fired but below the threshold. This is precisely the data that lets
        # the threshold be tuned later without re-crawling.
        near_miss["plausibility_near_miss"] = list(plausibility.fired)
    return Verdict.good(near_miss)


# --------------------------------------------------------------------------
# Thin content: empty bodies and navigation shells
# --------------------------------------------------------------------------
#
# Calibrated on real pages, 7 Sep 2026 (words / median line chars / share of
# lines ending a sentence / share of lines over 60 chars):
#
#     ancestry.com shell         96 /  9.5 / 0.00 / 0.00   <- the positive
#     example.com                20 / 46.0 / 0.33 / 0.33
#     dictionary-site/guides    329 / 99.5 / 0.50 / 0.50
#     dictionary-site/entries  1720 / 80.0 / 0.94 / 0.77
#     python.org (nav-heavy)   1092 / 18.0 / 0.10 / 0.24   <- hardest negative
#     imdb.com fallback         336 / 15.0 / 0.01 / 0.00   <- was MISSED
#     imdb.com structured       207 / 99.0 / 0.00 / 0.83   <- the good capture
#
# A shell is short lines, none of them long, and none of them sentences. Every
# one of those has to hold at once: python.org has short lines too, but a
# fifth of them are long and a tenth end sentences.
#
# THERE IS NO LONGER A WORD CAP, and that is the point. IMDb's homepage fell
# back to a 336-word dump of the menu and the language picker — "Menu | All |
# Watchlist | Sign in | Create account | EN | Français (Canada) | …" — and was
# billed at 5 credits as content, because 336 was sixteen words over a 320
# ceiling. Raising the ceiling would have been a patch waiting to be walked
# past again by a site with one more language.
#
# The cap earned nothing: every negative in the table above is excluded three
# times over on shape alone. python.org fails the median, the long share AND
# the sentence share; the dictionary pages fail all three. A document of any length whose
# lines are short, never long and never sentences is a menu, and word count
# was the weakest of the five tests while being the only one IMDb passed.
NAV_SHELL_MIN_LINES = 8
NAV_SHELL_MAX_MEDIAN_LINE = 15
NAV_SHELL_MAX_LONG_LINE_SHARE = 0.02
NAV_SHELL_MAX_SENTENCE_SHARE = 0.05
_SENTENCE_END = re.compile(r"[.!?…]$")


def _shape(markdown: str) -> dict[str, float]:
    lines = [ln.strip() for ln in markdown.splitlines() if ln.strip()]
    if not lines:
        return {"lines": 0, "median": 0.0, "long": 0.0, "sentences": 0.0}
    lengths = sorted(len(ln) for ln in lines)
    median = float(lengths[len(lengths) // 2])
    return {
        "lines": len(lines),
        "median": median,
        "long": sum(1 for ln in lines if len(ln) > 60) / len(lines),
        "sentences": sum(1 for ln in lines if _SENTENCE_END.search(ln)) / len(lines),
    }


def is_nav_shell(markdown: str, word_count: int, extraction_path: str = "") -> bool:
    """A menu with no page behind it, judged on shape alone so it works cold."""
    _ = (word_count, extraction_path)  # judged on shape alone; see the note above
    shape = _shape(markdown)
    return (
        shape["lines"] >= NAV_SHELL_MIN_LINES
        and shape["median"] <= NAV_SHELL_MAX_MEDIAN_LINE
        and shape["long"] <= NAV_SHELL_MAX_LONG_LINE_SHARE
        and shape["sentences"] <= NAV_SHELL_MAX_SENTENCE_SHARE
    )


def _corroborate(verdict: Verdict, signature_hit: tuple[Signature, float] | None) -> Verdict:
    """A thin page is only a BLOCK when something else says a WAF handed it to us.

    "No content" is the one observation that means two completely different
    things: a Cloudflare Bot Fight interstitial, and a JavaScript app that has
    not rendered. Word count cannot tell them apart — and the two want opposite
    handling, because a block raises the domain's tier floor for a week and a
    thin page must never do that.

    A matched signature is the thing that separates them, and it corroborates
    even below the confidence gates above: `cf_challenge_platform_script` is
    0.6 precisely BECAUSE it is meaningless on its own, and thin content is the
    second half of the evidence it was waiting for.
    """
    if verdict.reason != Reason.THIN or signature_hit is None:
        return verdict
    signature, confidence = signature_hit
    return Verdict(
        ok=False,
        reason=Reason.SOFT_BLOCK,
        signal=verdict.signal,
        confidence=max(verdict.confidence, confidence),
        vendor=signature.vendor,
        details={**verdict.details, "signature": signature.id},
    )


def _thin(extraction: ExtractionSummary) -> Verdict | None:
    if extraction.char_count == 0:
        return Verdict(
            ok=False,
            reason=Reason.THIN,
            signal="empty_content",
            confidence=0.95,
            details={"char_count": 0},
        )
    if is_nav_shell(extraction.markdown, extraction.word_count, extraction.extraction_path):
        return Verdict(
            ok=False,
            reason=Reason.THIN,
            signal="nav_shell",
            confidence=0.85,
            details={"word_count": extraction.word_count, **_shape(extraction.markdown)},
        )
    return None
