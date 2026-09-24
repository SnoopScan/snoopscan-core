"""The API contract (01-api-surface.md), as Pydantic v2 models.

Frozen before anything is built behind it. Option names deliberately match
Firecrawl's public API where the concept is the same, so callers migrate by
changing a base URL.

Every request model sets `extra="forbid"`: an unknown field is a 400, never a
silent no-op. Accepting typos quietly causes long debugging sessions.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engine.settings import settings


class Strict(BaseModel):
    """Base for request models: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class Tier(StrEnum):
    HTTP = "http"
    IMPERSONATE = "impersonate"
    BROWSER = "browser"
    STEALTH = "stealth"
    STEALTH_HARD = "stealth_hard"
    MOBILE = "mobile"


# Ladder order for escalation. `mobile` sits above stealth_hard.
TIER_ORDER: tuple[Tier, ...] = (
    Tier.HTTP,
    Tier.IMPERSONATE,
    Tier.BROWSER,
    Tier.STEALTH,
    Tier.STEALTH_HARD,
    Tier.MOBILE,
)


class ProxyMode(StrEnum):
    NONE = "none"
    DATACENTER = "datacenter"
    RESIDENTIAL = "residential"
    MOBILE = "mobile"
    AUTO = "auto"


class PageType(StrEnum):
    ARTICLE = "article"
    DOCS = "docs"
    FORUM = "forum"
    PRODUCT = "product"
    LISTING = "listing"
    TABLE = "table"
    UNKNOWN = "unknown"


class ExtractionPath(StrEnum):
    HEURISTIC = "heuristic"
    STRUCTURED = "structured"
    FALLBACK = "fallback"
    # A PDF or DOCX read by the document parser rather than the HTML
    # extractor. Named so a caller can tell why a page has no links.
    PARSER = "parser"
    # A body that is already text: JSON, plain text, markdown. Returned exactly
    # as the server sent it, because the HTML converter escapes backslashes,
    # asterisks and underscores and so corrupts JSON and word lists.
    VERBATIM = "verbatim"


class JobKind(StrEnum):
    SCRAPE = "scrape"
    CRAWL = "crawl"
    BATCH = "batch"
    MAP = "map"
    EXTRACT = "extract"
    SEARCH = "search"
    LEADS = "leads"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# --------------------------------------------------------------------------
# Format specifications
# --------------------------------------------------------------------------


class MediaAsset(Strict):
    """One image, video or audio file the page references."""

    url: str
    type: Literal["image", "video", "audio"]
    alt: str | None = None


class ScreenshotFormat(Strict):
    type: Literal["screenshot"]
    fullPage: bool = False
    quality: Annotated[int, Field(ge=1, le=100)] = 80


class SchemaOrTemplate(Strict):
    """Structured fields: your own schema, or a named template.

    A template is a curated schema for a kind of page — `product`, `article`,
    `jobPosting` — with schema.org field names, which is what sites already
    publish in JSON-LD. That means the fields usually come from the page's own
    markup, free, with no model call: the caller writes no schema and pays no
    tokens for a price that was in the page all along.

    Shared by the `json` scrape format and by /extract, so a template means
    exactly the same thing on both. It lived only on the scrape format at
    first, which left the schema-first endpoint — the one a caller reaches for
    when they want fields — as the only place templates did not work.
    """

    schema_: dict[str, Any] | None = Field(
        default=None,
        alias="schema",
        description=(
            "Your own JSON Schema for the fields you want. Give this or `template`, not both."
        ),
    )
    template: str | None = Field(
        default=None,
        description=(
            "A named template instead of a schema — `product`, `article`, `jobPosting` "
            "and more. GET /v1/templates lists them with their fields."
        ),
    )

    @model_validator(mode="after")
    def _one_of_schema_or_template(self) -> SchemaOrTemplate:
        from engine.core.extract import templates as _templates

        if self.template is not None:
            if self.schema_ is not None:
                raise ValueError(
                    "Give `schema` or `template`, not both: a template IS a schema, "
                    "and silently picking one would return fields you did not ask for."
                )
            if _templates.schema_for(self.template) is None:
                available = ", ".join(_templates.names())
                raise ValueError(f"Unknown template {self.template!r}. Available: {available}.")
        elif self.schema_ is None:
            raise ValueError("The json format needs either a `schema` or a `template`.")
        return self

    @property
    def effective_schema(self) -> dict[str, Any]:
        """The schema to extract against, whichever way it was asked for."""
        if self.template is not None:
            from engine.core.extract import templates as _templates

            found = _templates.schema_for(self.template)
            if found is not None:
                return found
        return self.schema_ or {}


class JsonFormat(SchemaOrTemplate):
    type: Literal["json"]
    prompt: str | None = None


DiffMode = Literal["git-diff", "json"]


def _default_diff_modes() -> list[DiffMode]:
    return ["git-diff"]


class ChangeTrackingFormat(Strict):
    type: Literal["changeTracking"]
    modes: list[DiffMode] = Field(default_factory=_default_diff_modes)


SimpleFormat = Literal[
    "markdown", "html", "rawHtml", "links", "media", "summary", "screenshot", "json", "network"
]
ParserName = Literal["pdf"]
WebhookEvent = Literal["started", "page", "completed", "failed"]
SearchSource = Literal["web", "news", "images"]
SearchDevice = Literal["desktop", "mobile"]
SearchFreshness = Literal["hour", "day", "week", "month", "year"]
SearchSafety = Literal["off", "moderate", "strict"]
FormatSpec = SimpleFormat | ScreenshotFormat | JsonFormat | ChangeTrackingFormat


# Default factories are named functions rather than lambdas so their element
# types are the Literal types above; a bare lambda widens to list[str].
def _default_formats() -> list[FormatSpec]:
    return ["markdown"]


def _default_parsers() -> list[ParserName]:
    return ["pdf"]


def _default_webhook_events() -> list[WebhookEvent]:
    return ["completed", "failed"]


def _default_search_sources() -> list[SearchSource]:
    return ["web"]


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


class WaitAction(Strict):
    type: Literal["wait"]
    milliseconds: int | None = None
    selector: str | None = None

    @model_validator(mode="after")
    def one_of(self) -> WaitAction:
        if (self.milliseconds is None) == (self.selector is None):
            raise ValueError("wait action needs exactly one of milliseconds or selector")
        return self


class ClickAction(Strict):
    type: Literal["click"]
    selector: str


class CaptchaCheckboxAction(Strict):
    """One explicit widget click; expected page content, not a tick, is the goal."""

    type: Literal["captchaCheckbox"]
    frameSelector: str = 'iframe[src*="/recaptcha/"][src*="/anchor"]'
    selector: str = "#recaptcha-anchor"
    successSelector: Annotated[str, Field(min_length=1)]
    timeoutMs: Annotated[int, Field(ge=500, le=15_000)] = 8_000
    captureEvidence: bool = Field(
        False,
        description="Return before/after screenshots and heuristic challenge classification.",
    )


class WriteAction(Strict):
    type: Literal["write"]
    selector: str
    text: str


class PressAction(Strict):
    type: Literal["press"]
    key: str


class ScrollAction(Strict):
    type: Literal["scroll"]
    direction: Literal["up", "down"] = "down"
    amount: Annotated[int, Field(ge=1, le=50)] = 1


class ScreenshotAction(Strict):
    type: Literal["screenshot"]
    fullPage: bool = False


class ScrapeAction(Strict):
    type: Literal["scrape"]


class ExecuteJavascriptAction(Strict):
    type: Literal["executeJavascript"]
    script: str


Action = Annotated[
    WaitAction
    | ClickAction
    | CaptchaCheckboxAction
    | WriteAction
    | PressAction
    | ScrollAction
    | ScreenshotAction
    | ScrapeAction
    | ExecuteJavascriptAction,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------
# Shared option objects
# --------------------------------------------------------------------------


class Location(Strict):
    country: Annotated[str, Field(min_length=2, max_length=2)]
    languages: list[str] | None = None

    @field_validator("country")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.upper()


# Headers owned by the fetch tier. A caller-set value here contradicts the TLS
# and HTTP/2 fingerprint, which is precisely the mismatch detectors look for.
FINGERPRINT_HEADERS = frozenset(
    {
        "user-agent",
        "accept",
        "accept-language",
        "accept-encoding",
    }
)
FINGERPRINT_HEADER_PREFIXES = ("sec-ch-",)


# A field the public reference does not print. The tier controls are a real
# part of the contract and keep working; naming their values on a marketing
# site would publish the order we try things in, which is the one thing our
# customers' targets would like to read. Marked, not removed.
# dict[str, Any], not dict[str, bool]: pydantic's json_schema_extra takes a
# JsonDict (dict[str, JsonValue]), and dict is invariant in its value type —
# dict[str, bool] is not a JsonDict even though bool is a valid JSON value.
INTERNAL: dict[str, Any] = {"x-internal": True}


class ScrapeOptions(Strict):
    """Every /v1/scrape field except `url`. Reused by crawl/batch/extract.

    Descriptions live HERE, on the model, so they reach OpenAPI and from there
    the published reference. A field added without one fails its own test
    rather than shipping undocumented.
    """

    formats: list[FormatSpec] = Field(
        default_factory=_default_formats,
        description=(
            "What to return: `markdown`, `html`, `rawHtml`, `links`, `media`, `summary`, "
            "`screenshot`, or a `json` object carrying your schema. Ask for several in one call."
        ),
    )

    onlyMainContent: bool = Field(
        True,
        description=(
            "Return the article and drop navigation, headers, footers and cookie banners. "
            "Turn it off to keep the whole page."
        ),
    )
    includeTags: list[str] = Field(
        default_factory=list,
        description="CSS selectors to keep even when they sit outside the main content.",
    )
    excludeTags: list[str] = Field(
        default_factory=list,
        description="CSS selectors to drop before the page is read.",
    )

    maxAge: Annotated[int, Field(ge=0)] = Field(
        settings.default_max_age_ms,
        description=(
            "Serve a cached copy if one was taken within this many milliseconds. "
            "A cache hit costs nothing. Set `0` to force a fresh fetch."
        ),
    )
    storeInCache: bool = Field(
        True,
        description="Keep this response in the cache so a later request can be served free.",
    )

    waitFor: Annotated[int, Field(ge=0, le=120_000)] = Field(
        0,
        description=(
            "Milliseconds to wait after the page loads before reading it, for content that "
            "arrives late. Needs a real browser, so it is priced as a browser fetch."
        ),
    )
    timeout: Annotated[int, Field(ge=1_000, le=300_000)] = Field(
        settings.default_timeout_ms,
        description="Give up on this request after this many milliseconds.",
    )
    actions: list[Action] = Field(
        default_factory=list,
        description=(
            "Click, type, scroll, wait, or explicitly attempt captchaCheckbox in a real "
            "browser before the page is read. Checkbox attempts require successSelector."
        ),
    )

    captchaHandling: Literal["auto", "off"] = Field(
        "auto",
        description="Try one recognised challenge checkbox in browser modes; off disables it.",
    )
    captchaEvidence: bool = Field(
        False,
        description="Capture same-session screenshots for an automatic checkbox attempt.",
    )

    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Extra request headers. Headers that identify the client are set for you "
            "and cannot be overridden."
        ),
    )
    mobile: bool = Field(False, description="Read the page as a phone would see it.")
    location: Location | None = Field(
        None, description="Fetch from a particular country, with that country's language."
    )

    proxy: ProxyMode = Field(
        ProxyMode.AUTO,
        description="Leave on `auto` and the route is chosen for you.",
    )
    tier: Tier | Literal["auto"] = Field("auto", json_schema_extra=INTERNAL)
    escalate: bool = Field(True, json_schema_extra=INTERNAL)
    # The most expensive rung this request is willing to pay for. `escalate:
    # false` is the blunt version — one tier and stop — which is wrong for a
    # bulk pass that wants tier 0 or 1 and nothing dearer.
    maxTier: Tier | None = Field(None, json_schema_extra=INTERNAL)

    blockAssets: bool = Field(
        True,
        description=(
            "Skip images, fonts and media when a browser is used. Faster, and cheaper on bandwidth."
        ),
    )
    removeBase64Images: bool = Field(
        True, description="Strip inline base64 images out of the returned markdown and HTML."
    )

    # Unlisted, like the tier controls. Measured across seven competitors'
    # docs (6 Sep 2026): six do not mention robots.txt anywhere — they neither
    # promise to honour it nor advertise ignoring it. Publishing a default
    # posture states an intent in the least helpful possible place, so the
    # field keeps working and stops being a headline.
    respectRobots: bool = Field(False, json_schema_extra=INTERNAL)
    parsers: list[ParserName] = Field(
        default_factory=_default_parsers,
        description="Which document parsers may run, for example `pdf`.",
    )

    @field_validator("headers")
    @classmethod
    def reject_fingerprint_headers(cls, v: dict[str, str]) -> dict[str, str]:
        for name in v:
            lower = name.lower()
            if lower in FINGERPRINT_HEADERS or lower.startswith(FINGERPRINT_HEADER_PREFIXES):
                raise ValueError(
                    f"header '{name}' is owned by the fetch tier and cannot be overridden; "
                    "a mismatch with the TLS fingerprint is itself a detection signal"
                )
        return v

    @model_validator(mode="after")
    def json_needs_a_schema(self) -> ScrapeOptions:
        """`"json"` as a bare string has nowhere to put a schema.

        It was accepted and returned null forever, which reads as an engine
        fault rather than a request the caller can fix in one line. Saying so
        at the door costs them nothing; billing a fetch for a null costs them
        a credit and a support ticket.
        """
        if any(isinstance(f, str) and f == "json" for f in self.formats):
            raise ValueError(
                "the `json` format carries your schema, so it must be the object "
                'form: {"type": "json", "schema": {...}}'
            )
        return self

    @model_validator(mode="after")
    def change_tracking_needs_markdown(self) -> ScrapeOptions:
        wants_change_tracking = any(isinstance(f, ChangeTrackingFormat) for f in self.formats)
        if wants_change_tracking and not self._has_format("markdown"):
            raise ValueError("changeTracking requires markdown to also be requested")
        return self

    @property
    def format_names(self) -> list[str]:
        """The requested formats as plain names, sorted.

        `formats` is a mixed list — bare strings and format OBJECTS — so it
        cannot be sorted, compared or logged directly: sorting str against a
        model raises TypeError. It only raises with two or more entries,
        because a one-element sort never compares anything, which is why
        `["markdown"]` and `[{"type": "screenshot"}]` each worked and asking
        for both together returned a 500.
        """
        return sorted(f if isinstance(f, str) else str(getattr(f, "type", f)) for f in self.formats)

    def _has_format(self, name: str) -> bool:
        for f in self.formats:
            if isinstance(f, str) and f == name:
                return True
            if not isinstance(f, str) and getattr(f, "type", None) == name:
                return True
        return False

    @property
    def wants_screenshot(self) -> bool:
        return self._has_format("screenshot")

    @property
    def wants_network(self) -> bool:
        return self._has_format("network")

    @property
    def screenshot_format(self) -> ScreenshotFormat | None:
        """The screenshot options, when the caller passed the object form.

        `formats: ["screenshot"]` is the shorthand and carries no options;
        `formats: [{"type": "screenshot", "fullPage": true}]` carries them.
        """
        for f in self.formats:
            if isinstance(f, ScreenshotFormat):
                return f
        return None

    @property
    def json_format(self) -> JsonFormat | None:
        for f in self.formats:
            if isinstance(f, JsonFormat):
                return f
        return None

    @property
    def change_tracking(self) -> ChangeTrackingFormat | None:
        for f in self.formats:
            if isinstance(f, ChangeTrackingFormat):
                return f
        return None

    @model_validator(mode="after")
    def one_checkbox_attempt(self) -> ScrapeOptions:
        if sum(a.type == "captchaCheckbox" for a in self.actions) > 1:
            raise ValueError("at most one captchaCheckbox action is allowed per request")
        return self

    @property
    def has_captcha_checkbox(self) -> bool:
        return any(a.type == "captchaCheckbox" for a in self.actions)

    @property
    def forces_browser(self) -> bool:
        """Screenshot, network, actions or a wait cannot be satisfied below the
        browser tier — the plain tiers load one document and run no scripts, so
        there is no page activity to photograph or log.

        `waitFor` used to be accepted and silently dropped by the plain tiers —
        a caller asking for five seconds of settling got a request that could
        not wait at all, and no sign that it had not.
        """
        return bool(self.actions) or self.wants_screenshot or self.wants_network or self.waitFor > 0

    @property
    def tier_floor(self) -> Tier | None:
        """The cheapest rung that can actually honour this request.

        One property, because the answer kept being needed and kept being
        expressed as a different boolean. `mobile` was the case that showed
        it: tier 0 sends our honest bot User-Agent and cannot be a phone, and
        tier 1 impersonates a real browser and can — so `mobile: true` was
        accepted, billed, and answered with the desktop page whenever tier 0
        happened to work. Measured against httpbin.org/headers, 9 Sep 2026:
        the User-Agent was byte-identical with `mobile` on and off.
        """
        if self.forces_browser:
            return Tier.BROWSER
        if self.mobile:
            return Tier.IMPERSONATE
        return None


class CompanyRequest(Strict):
    """Enrich one company from its own website.

    A domain or any URL on it, and back come the firmographics a lead list
    actually sells — name, phone, address, LinkedIn, headcount, industry — plus
    the contact emails, social links and contact form the site publishes. It is
    the single-company half of the lead pipeline: no directory, no list, just
    "tell me everything this one site says about itself".
    """

    url: str = Field(
        description=(
            "A company domain, or any URL on it. `https://acme.com/about` and `acme.com` "
            "enrich the same company."
        )
    )
    contacts: bool = Field(
        True,
        description=(
            "Discover contact emails, the contact form and social links, "
            "not just the firmographics."
        ),
    )


class DomainRequest(Strict):
    """What to look up about a domain. Every part is opt-out, not opt-in: a
    caller asking about a domain wants what is known about it, and making them
    enumerate the pieces is a worse default than sending three cheap lookups."""

    domain: str = Field(
        description=(
            "A domain, or any URL on it. `https://www.example.com/a` and `example.com` "
            "are the same request."
        )
    )
    registration: bool = Field(
        True, description="Registration and expiry dates, age, registrar and nameservers."
    )
    dns: bool = Field(True, description="A, AAAA, MX, NS and TXT records.")
    backlinks: bool = Field(
        True, description="Which domains SnoopScan has seen linking to this one."
    )
    backlinkLimit: Annotated[int, Field(ge=1, le=1000)] = Field(
        100, description="How many referring domains to list, busiest first."
    )


class ScrapeRequest(ScrapeOptions):
    url: str = Field(description="The absolute http or https URL to read.")

    @field_validator("url")
    @classmethod
    def absolute_http(cls, v: str) -> str:
        v = v.strip()
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("url must be an absolute http or https URL")
        return v


# --------------------------------------------------------------------------
# Cost accounting (constraint C4)
# --------------------------------------------------------------------------


class Cost(BaseModel):
    """What a response actually cost to produce.

    Failed requests carry no cost and are never counted against a quota.
    """

    model_config = ConfigDict(extra="forbid")

    tier: str | None = None
    tiers_attempted: list[str] = Field(default_factory=list)
    proxy_used: bool = False
    proxy_type: str | None = None
    proxy_bytes: int = 0
    browser_ms: int = 0
    extraction_path: str | None = None
    credits: int | None = None  # what this response was charged, filled in by billing
    cached: bool = False
    # True only when the cached row was populated by THIS owner's own spend.
    # A hit on somebody else's row is still a cache hit — instant, no fetch —
    # but it is not work this customer paid for, and pricing it at zero means
    # the more popular a URL is the less we earn on it.
    cache_own: bool = False
    # Pages parsed from a PDF, billed per page. Zero until the PDF parser lands.
    pdf_pages: int = 0
    # Flat-priced work that is not a fetch: {"search": 1}, {"map": 1},
    # {"model_extract": 1}. Keys are pricing-table keys; a fetch's base cost is
    # added only when a fetch actually happened (tier set, or a cache hit).
    extras: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def from_cache(
        cls,
        tier: str | None = None,
        tiers_attempted: list[str] | None = None,
        extraction_path: str | None = None,
        *,
        cache_own: bool = False,
    ) -> Cost:
        """A cache hit costs nothing now — but it cost something once.

        Carrying the original accounting forward keeps the cost object honest
        about what the page took to fetch. A null tier hid it (measured
        §8); carrying the tier alone still left `tiers_attempted: []` and
        `extraction_path: null`, which reads as "no fetch ever happened"
        (also measured). The page row stores all three.
        """
        return cls(
            cached=True,
            cache_own=cache_own,
            tier=tier,
            tiers_attempted=list(tiers_attempted or ([tier] if tier else [])),
            extraction_path=extraction_path,
        )


class PageMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    language: str | None = None
    author: str | None = None
    publishedAt: str | None = None
    sourceURL: str
    url: str
    statusCode: int | None = None
    contentType: str | None = None
    pageType: str = PageType.UNKNOWN
    wordCount: int = 0
    extractionConfidence: float = 0.0
    # shopify, wordpress, woocommerce… detected from the page; None when unknown.
    platform: str | None = None


class NetworkRequest(BaseModel):
    """One request the page made while it loaded."""

    model_config = ConfigDict(extra="forbid")

    url: str
    method: str
    type: str  # Playwright's resource type: document, script, xhr, fetch, image, ping ...
    status: int | None = None
    failure: str | None = None


class TrackerHit(BaseModel):
    """An ad or analytics tag seen in the page's traffic.

    `kind` separates INSTALLED from FIRED: `loaded` means the vendor's library
    was fetched, `hit` means a collection request actually went out. Ad
    verification is about hits.
    """

    model_config = ConfigDict(extra="forbid")

    vendor: str  # ga4, gtm, google_ads, meta, tiktok, linkedin, microsoft_ads
    kind: Literal["loaded", "hit"]
    id: str | None = None
    event: str | None = None
    status: int | None = None
    # confirmed (2xx/3xx), refused (error status or never arrived), unconfirmed
    # (went out, no answer seen — common for fire-and-forget beacons). The tag
    # FIRING is proven by the hit existing; delivery is the weaker claim.
    delivery: Literal["confirmed", "refused", "unconfirmed"] = "unconfirmed"
    failed: bool = False  # delivery == "refused"
    count: int = 1


class NetworkLog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trackers: list[TrackerHit] = Field(default_factory=list)
    requests: list[NetworkRequest] = Field(default_factory=list)
    # How many the page made, which can exceed len(requests) — the list is
    # capped so a page with ten thousand beacons cannot make a 50MB response.
    total: int = 0
    truncated: bool = False


class ActionResults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screenshots: list[str] = Field(default_factory=list)
    scrapes: list[dict[str, Any]] = Field(default_factory=list)
    javascriptReturns: list[dict[str, Any]] = Field(default_factory=list)
    captcha: list[dict[str, Any]] = Field(default_factory=list)


class ScrapeData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str | None = None
    html: str | None = None
    rawHtml: str | None = None
    links: list[str] | None = None
    media: list[MediaAsset] | None = None
    summary: str | None = None
    screenshot: str | None = None
    network: NetworkLog | None = None
    json_: dict[str, Any] | None = Field(default=None, serialization_alias="json")
    changeTracking: dict[str, Any] | None = None
    # Why something you asked for is not here. A format that cannot be
    # delivered used to return null with no explanation — `summary` and
    # `json` returned null on EVERY request for months and nothing said so.
    # Null plus a reason is honest; null on its own is indistinguishable
    # from a bug, and was one.
    warnings: list[str] | None = None
    actions: ActionResults | None = None
    # The structured product behind a Shopify or WooCommerce product page, from
    # the store's own JSON — price, variants, stock — beside the markdown.
    product: dict[str, Any] | None = None
    metadata: PageMetadata
    cost: Cost


# --------------------------------------------------------------------------
# Response envelope
# --------------------------------------------------------------------------


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    detail: dict[str, Any] | None = None


class SuccessEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: Literal[True] = True
    data: Any


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: Literal[False] = False
    error: ErrorBody


# --------------------------------------------------------------------------
# Crawl / map / batch / extract / search
# --------------------------------------------------------------------------


class WebhookConfig(Strict):
    url: str
    events: list[WebhookEvent] = Field(default_factory=_default_webhook_events)
    headers: dict[str, str] = Field(default_factory=dict)


class CrawlRequest(Strict):
    url: str = Field(description="The page to start from. An absolute http or https URL.")

    limit: Annotated[
        int, Field(ge=1, le=100_000, description="The most pages to crawl, 1 to 100,000.")
    ] = 100
    maxDepth: Annotated[
        int,
        Field(
            ge=0,
            le=20,
            description=(
                "How many links deep to follow from the start page. 0 is the start page alone."
            ),
        ),
    ] = 3
    maxConcurrency: Annotated[
        int, Field(ge=1, le=50, description="How many pages to fetch at once, 1 to 50.")
    ] = 5

    includePaths: list[str] = Field(
        default_factory=list,
        description=(
            "Only crawl URLs whose path and query match one of these regexes, e.g. `^/blog/`. "
            "Empty means every path."
        ),
    )
    excludePaths: list[str] = Field(
        default_factory=list,
        description=(
            "Skip URLs whose path and query match any of these regexes. Wins over `includePaths`."
        ),
    )

    allowExternalLinks: bool = Field(
        default=False,
        description=(
            "Follow links onto other domains. Off by default: with a high `limit` it can wander a"
            " long way."
        ),
    )
    allowBackwardLinks: bool = Field(
        default=False, description="Follow links above the starting path on the same host."
    )

    ignoreSitemap: bool = Field(
        default=False,
        description="Find pages by following links only, without reading the site's sitemap.",
    )
    ignoreQueryParameters: bool = Field(
        default=False,
        description="Treat URLs that differ only in their query string as the same page.",
    )
    deduplicateSimilarURLs: bool = Field(
        default=True,
        description="Collapse URLs that differ only in tracking parameters or a trailing slash.",
    )

    delay: Annotated[
        int,
        Field(
            ge=0,
            le=60_000,
            description="Minimum milliseconds between requests to the same host.",
        ),
    ] = 0
    # A crawl follows the same rule as a scrape: the operator decides, not the
    # target. Set `true` per request to honour robots on a given job.
    respectRobots: bool = Field(
        default=False, description="Honour the site's robots.txt for this job."
    )

    scrapeOptions: ScrapeOptions = Field(
        default_factory=ScrapeOptions,
        description=(
            "How each page is fetched and what comes back: the same options /v1/scrape takes."
        ),
    )
    webhook: WebhookConfig | None = Field(
        default=None, description="Where to send job events as the crawl progresses."
    )

    @field_validator("url")
    @classmethod
    def absolute_http(cls, v: str) -> str:
        if not v.strip().lower().startswith(("http://", "https://")):
            raise ValueError("url must be an absolute http or https URL")
        return v.strip()

    @field_validator("includePaths", "excludePaths")
    @classmethod
    def valid_regex(cls, v: list[str]) -> list[str]:
        import re

        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex '{pattern}': {exc}") from exc
        return v


class MapRequest(Strict):
    url: str = Field(description="The site to map. An absolute http or https URL.")
    search: str | None = Field(
        default=None,
        description=(
            "Only return URLs whose address or page title contains this text. Not case-sensitive."
        ),
    )
    limit: Annotated[
        int, Field(ge=1, le=30_000, description="The most URLs to return, 1 to 30,000.")
    ] = 5_000
    includeSubdomains: bool = Field(
        default=False, description="Include URLs on the site's subdomains."
    )
    ignoreSitemap: bool = Field(
        default=False, description="Find URLs without reading the site's sitemap."
    )


class MapLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    title: str | None = None
    source: Literal["sitemap", "crawl", "robots"]


class BatchScrapeRequest(Strict):
    urls: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=10_000,
            description="The pages to scrape as one job, 1 to 10,000.",
        ),
    ]
    maxConcurrency: Annotated[
        int, Field(ge=1, le=50, description="How many pages to fetch at once, 1 to 50.")
    ] = 10
    scrapeOptions: ScrapeOptions = Field(
        default_factory=ScrapeOptions,
        description=(
            "How each page is fetched and what comes back: the same options /v1/scrape takes."
        ),
    )
    webhook: WebhookConfig | None = Field(
        default=None, description="Where to send job events as pages complete."
    )


class ModelSpec(Strict):
    """The customer's own model for /v1/extract: used for this request, never stored."""

    provider: Literal["anthropic", "openai"]
    apiKey: Annotated[str, Field(min_length=8, max_length=300)]
    name: Annotated[str, Field(min_length=1, max_length=100)] | None = None

    def __repr__(self) -> str:  # never in a log or an error
        return f"ModelSpec(provider={self.provider!r}, name={self.name!r}, apiKey='***')"


class ExtractRequest(SchemaOrTemplate):
    urls: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=100,
            description=(
                "The pages to extract from, 1 to 100. Each is fetched and answers for itself."
            ),
        ),
    ]
    prompt: str | None = Field(
        default=None,
        description='Plain-language guidance on what to pick out, e.g. "the cheapest paid plan".',
    )
    scrapeOptions: ScrapeOptions = Field(
        default_factory=ScrapeOptions,
        description="How each page is fetched: the same options /v1/scrape takes.",
    )
    # Bring your own key: the model then costs the customer, not the operator,
    # and the model_extract surcharge is not applied.
    model: ModelSpec | None = Field(
        default=None,
        description=(
            "Your own model provider and key. Fields the page's markup cannot answer go to it, on"
            " your key, at no credit cost."
        ),
    )


class SearchRequest(Strict):
    """A search, with the knobs a real caller turns.

    Every field below `location` is optional and, when set, is a REQUIREMENT
    rather than a preference: a source that cannot honour it is skipped rather
    than answering with something else. Mobile results are a different page
    from desktop ones, and quietly serving the wrong one is the kind of wrong
    that nobody catches.
    """

    query: str = Field(description="What to search for.")
    limit: Annotated[int, Field(ge=1, le=50, description="How many results, 1 to 50.")] = 10
    sources: list[SearchSource] = Field(
        default_factory=_default_search_sources,
        description="Which kinds of result: `web`, `news`, `images`.",
    )
    location: Location | None = Field(
        default=None,
        description=(
            'The country to search from, which changes the results, e.g. `{"country": "GB"}`.'
        ),
    )
    scrapeOptions: ScrapeOptions | None = Field(
        default=None,
        description=(
            "Also fetch every result page with these options. Each page fetched is charged like a"
            " scrape."
        ),
    )

    # Where the searcher is standing, in Google's own free-text form —
    # "Austin, Texas, United States". Finer than `location.country`: local
    # results, stock and pricing all move with it.
    place: str | None = Field(
        default=None,
        description=(
            'Where the searcher is standing, in free text, e.g. "Austin, Texas, United States". '
            "Finer than `location`."
        ),
    )
    # BCP-47-ish language code the results should be in.
    language: Annotated[str, Field(min_length=2, max_length=8)] | None = Field(
        default=None, description="The language results should be in, e.g. `en`."
    )
    # Mobile SERPs are a different page, not a reflow of the desktop one.
    device: SearchDevice | None = Field(
        default=None,
        description=(
            "`desktop` or `mobile`. Mobile results are a different page, not a reflow of the "
            "desktop one."
        ),
    )
    # Only results from the last hour/day/week/month/year.
    freshness: SearchFreshness | None = Field(
        default=None,
        description="Only results from the last `hour`, `day`, `week`, `month` or `year`.",
    )
    safeSearch: SearchSafety | None = Field(
        default=None, description="`off`, `moderate` or `strict`."
    )
    # 1-based. Page 2 is the second page of results, not an offset.
    page: Annotated[
        int, Field(ge=1, le=100, description="Which page of results, starting at 1.")
    ] = 1
    # False stops the engine "correcting" the query — which matters when the
    # query is a part number, a misspelt brand, or the typo you are studying.
    autoCorrect: bool = Field(
        default=True,
        description=(
            "Set `false` to stop the query being spell-corrected, for a part number, a brand name"
            " or the typo you are studying."
        ),
    )


class JobSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: JobStatus
    url: str | None = None
    total: int = 0
    completed: int = 0
    failed: int = 0
    creditsUsed: int = 0
    cost: dict[str, Any] = Field(default_factory=dict)
    startedAt: str | None = None
    completedAt: str | None = None
    next: str | None = None
    data: list[Any] = Field(default_factory=list)
