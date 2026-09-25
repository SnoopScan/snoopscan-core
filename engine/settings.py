"""Single configuration source. Environment variables only (constraint C3).

No config reads scattered through the code — everything routes through the
`settings` singleton. Nothing here has a personal or identifying default.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENGINE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database / cache -------------------------------------------------
    database_url: str = Field(
        default="postgresql://localhost:5432/scraping_engine",
        description="asyncpg-compatible DSN. postgresql:// scheme.",
    )
    redis_url: str = Field(default="redis://localhost:6379/0")

    # --- Fetching ---------------------------------------------------------
    # Honest tier-0 identity (P: identify honestly).
    #
    # No version number. It goes out to every site we touch on this rung, and
    # a version in a public User-Agent is only ever read as "how finished is
    # this" — `0.1` invited exactly the wrong answer. Nothing about the
    # request depends on it, and bumping it would change the token an
    # operator has written into their own robots.txt.
    #
    # The contact URL RESOLVES. It was `https://example.invalid/bot-info`,
    # which is the RFC-reserved never-resolves domain and correct as a
    # placeholder — and it was the shipped default, so every site we touched
    # was handed a dead address to complain to. The `+` prefix is the
    # convention (Googlebot, bingbot, CCBot): "more about this agent here".
    # The slug is `/bot` because that is the shape of the field — google.com
    # /bot.html, yandex.com/bots, ahrefs.com/robot, brightdata.com/brightbot
    # — and it is what someone guesses after reading the token.
    user_agent: str = Field(
        default="SnoopScan (+https://snoopscan.com/bot)",
        description="Honest identifying UA for tier 0. Override via env in deploy.",
    )
    # RDAP, the protocol that replaced WHOIS. rdap.org is the community
    # redirector that forwards to whichever registry owns the TLD, so one base
    # URL covers every domain. Configurable because a self-hoster may prefer
    # to bootstrap from IANA's own list, and because a redirector is a
    # dependency worth being able to move off.
    rdap_base_url: str = Field(default="https://rdap.org")

    # Pinned curl_cffi impersonation profiles — never float (03-fetch-tiers s3).
    impersonate_profile: str = Field(default="chrome124")
    # The phone. `mobile: true` promises the page as a phone sees it, and a
    # site decides that from the User-Agent and the TLS fingerprint together —
    # so it has to be a real mobile profile, not a desktop one wearing a
    # different UA string. Pinned for the same reason as the desktop one.
    impersonate_profile_mobile: str = Field(default="chrome131_android")

    connect_timeout_ms: int = 10_000
    # The tier minimums sum to 67s; at 60s the sixth rung could never be
    # reached and a domain that needs stealth_hard (ancestry) always ended at
    # EXTRACTION_FAILED. 90s fits the whole ladder with room to spare.
    default_timeout_ms: int = 90_000
    # Five was tight: consent walls, country gates and shorteners routinely
    # chain three or four before the real page, and the redirect budget was
    # being spent before the fetch began.
    max_redirects: int = 10

    # The connection pool must be at least as large as the worker pool, or the
    # fetcher refuses work the worker was told to do. 07-orchestration.md sizes
    # HTTP workers at 50-200 concurrent; httpx defaults to 100, so at the top
    # of that range the engine failed 90% of requests with "too many
    # connections" — measured, not theorised. Sized above the spec's peak so
    # the worker pool is the limit rather than the transport.
    http_max_connections: int = 250
    http_max_keepalive: int = 50

    # Every in-flight scrape holds a politeness slot, and every slot is a Redis
    # connection. redis-py defaults its async pool to 100, so at the top of the
    # spec's 50-200 worker range the engine failed 90% of requests with "too
    # many connections" — the politeness gate, which exists to protect targets,
    # taking the engine down instead. Sized above the peak for the same reason
    # as the HTTP pool: the worker count should be the limit, not the plumbing.
    redis_max_connections: int = 300

    # --- Stealth tiers (03-fetch-tiers.md section 5) ---------------------
    stealth_enabled: bool = True
    # Full Chromium in new-headless mode, not the headless shell. Set false on
    # a host with a display for the headed posture Patchright recommends.
    # A wrapper that launches the browser under its OWN user, so the
    # crawler's loopback exceptions (postgres, redis, searxng) do not apply
    # to it. Empty means launch the browser the ordinary way, as the engine's
    # own user — which is what a self-hosted deployment with no egress filter
    # wants. See deploy/egress/README.md, "Browser isolation".
    browser_executable: str = Field(default="")
    stealth_headless: bool = True
    stealth_channel: str = Field(default="chromium")
    # How long tier 3 lets a JS challenge resolve itself before giving up. A
    # Cloudflare managed challenge clears in 3-8s for a browser it trusts; an
    # interactive Turnstile never will, and we do not solve CAPTCHAs.
    stealth_challenge_wait_ms: int = 12_000
    # Tier 3h / 4 need Camoufox installed (pip install camoufox[geoip];
    # python -m camoufox fetch). Absent, those rungs are simply not offered.
    stealth_hard_enabled: bool = True
    # Which rungs the crawl worker may climb. The spec splits HTTP and browser
    # work into separate containers (07-orchestration.md), so the default is
    # tiers 0/1. A single-box deployment sets "http,impersonate,browser,stealth"
    # and the same worker climbs — otherwise a JS-shell page in a crawl is an
    # honest failure the single-scrape endpoint would have rendered (§2).
    worker_tiers: str = Field(default="http,impersonate")
    # A WAF that is going to refuse a plain request does it in well under a
    # second. Fifteen seconds each here was 30s of a 60s budget spent learning
    # nothing, which is why the ladder was three rungs deep in practice.
    tier0_max_ms: int = 6_000
    tier1_max_ms: int = 6_000

    # --- Politeness -------------------------------------------------------
    # These are the FREE plan's pacing, and the fallback when a request arrives
    # with no plan attached (an internal call, a worker with no key context).
    politeness_default_delay_ms: int = 1_000
    politeness_default_concurrency: int = 2

    # Per-host pacing scales with what the caller pays for. A customer buying
    # 150 concurrent requests was still getting 2 against any single site and
    # one request a second, which is the free plan's pacing at 37x the price.
    #
    # The delay is the binding constraint — the gate serialises issue times —
    # so the floor is what actually caps throughput per host: 100ms is 10
    # requests a second, which large sites do not notice and small ones can
    # still take. It is a floor and not a target: a domain that has answered
    # 429 keeps its raised delay whatever the plan.
    politeness_floor_delay_ms: int = 100
    politeness_reference_concurrency: int = 5  # the free plan; the curve's anchor
    politeness_max_host_concurrency: int = 32
    politeness_host_share: int = 4  # a host may take a quarter of the plan's budget

    # --- Caching ----------------------------------------------------------
    # 0: a scrape with no explicit maxAge fetches fresh. It defaulted to 48h
    # because "a cache hit costs nothing" — but the customer paying for the
    # call wants today's page, and a two-day-old copy returned as success is
    # a quality fault dressed as an optimisation (it handed us a stale answer
    # mid-debug on 15 Sep 2026). Callers who WANT the cheap copy still ask for
    # it, and the paths that genuinely benefit already set their own: crawl
    # uses an hour, monitor uses 0. storeInCache stays on, so an opt-in hit is
    # still free.
    default_max_age_ms: int = 0

    # --- Circuit breaker --------------------------------------------------
    circuit_failure_rate: float = 0.5
    circuit_window: int = 20
    circuit_open_minutes: int = 15
    # Each reopen without a success in between doubles the last duration, up
    # to this. A domain that has never worked at any rung ends up probed once
    # a day instead of sixteen times an hour: wisdomlib.org took 542 attempts,
    # 70 minutes of fetch time and 14.7 MB of residential bandwidth over 33
    # hours with zero successes, and the breaker never opened once.
    circuit_open_max_minutes: int = 1_440

    # --- Security ---------------------------------------------------------
    # Fernet key (base64, 32 bytes) for proxy credential encryption at rest.
    encryption_key: str = Field(default="")
    # Toggle SSRF guard off only in isolated test environments.
    ssrf_guard_enabled: bool = True

    # --- Egress -----------------------------------------------------------
    # Whether a fetch may leave from THIS HOST'S OWN ADDRESS.
    #
    # True is right for a laptop and wrong for production. In production the
    # address customers connect to and the address targets see must not be the
    # same one: a single abusive customer puts it on a WAF reputation list and
    # tiers 0 and 1 stop working for everybody, abuse reports arrive at the
    # host running the API, and any caller can read the address back by
    # scraping an echo service. Firecrawl publishes exactly this split — no
    # fixed outbound set, identity asserted by User-Agent, one static address
    # for webhooks only.
    #
    # Set false on a deployment and every path that would have gone direct
    # refuses instead, the same way an explicit `proxy:` request already fails
    # closed rather than quietly leaving from our own IP.
    allow_direct_egress: bool = True

    def assert_egress_is_servable(self) -> None:
        """A deployment that forbids direct egress needs somewhere to egress TO.

        With `allow_direct_egress=False` and no proxy configured, every fetch is
        refused — a service that is up, healthy and incapable of doing its job.
        Fail at boot, where an operator is watching, rather than on each request.
        """
        if not self.allow_direct_egress and not self.proxy_enabled:
            raise RuntimeError(
                "allow_direct_egress=false requires proxy_enabled=true: with neither, "
                "every fetch is refused and the engine can serve nothing"
            )

    # --- Proxies (06-proxy-layer.md) --------------------------------------
    # Credentials come from the environment ONLY (constraint C3). Nothing here
    # has a real default; an unset username means the vendor is not configured
    # and the engine goes direct.
    proxy_enabled: bool = False

    # Residential endpoint: your provider's gateway host and port.
    proxy_residential_host: str = Field(default="")
    proxy_residential_port: int = 0
    proxy_username: str = Field(default="")
    proxy_password: str = Field(default="")
    proxy_country: str = Field(default="")

    # Datacenter endpoint, if the vendor plan includes one. Cheaper per GB and
    # fine for cooperative sites; higher detection risk on protected ones.
    proxy_datacenter_host: str = Field(default="")
    proxy_datacenter_port: int = 0
    proxy_datacenter_username: str = Field(default="")
    proxy_datacenter_password: str = Field(default="")

    # Residential vendors encode options (country, session) into the
    # credentials rather than as separate parameters — but they disagree about
    # WHICH field. Some providers append them to the PASSWORD; others use the
    # username. Both are templated so either style is a config edit, not a
    # deploy, and a vendor changing its format does not need a code change.
    #
    # Placeholders: {username} {password} {country} {session} {lifetime}
    # A segment whose value is empty is dropped, so a blank country does not
    # render as a dangling "_country-".
    proxy_username_template: str = Field(default="{username}")
    proxy_password_template: str = Field(default="{password}_country-{country}")
    proxy_password_sticky_template: str = Field(
        default="{password}_country-{country}_session-{session}_lifetime-{lifetime}m"
    )
    proxy_sticky_lifetime_minutes: int = 10
    # Where the environment-configured provider sits in the SAME priority order
    # as the desk-managed registry. Without this the registry short-circuited
    # the environment entirely, so adding any provider on the desk silently
    # took all traffic from the one in .env (measured 7 Sep 2026). Lower wins.
    proxy_priority: int = 100

    # --- What a GB costs, per provider and per type -------------------------
    # USD per GB. Within a priority tier the router sends a request to the
    # CHEAPEST healthy provider (providers.pick), and the spend report prices
    # the ledger with the same figures, so ordering and reporting cannot
    # disagree. Desk providers carry their own `cost_per_gb`; these two are
    # for the environment's gateways. Unset means "not priced": the type's
    # estimate below stands in.
    proxy_cost_per_gb: float | None = None
    proxy_datacenter_cost_per_gb: float | None = None
    # List-price ESTIMATES for a provider nobody has priced, Sep 2026 market
    # rates for pay-as-you-go plans. Used to order and to estimate only; the
    # desk's figure always wins. The order is what matters most: datacenter <
    # ISP < residential < mobile.
    proxy_default_cost_per_gb_datacenter: float = 0.60
    proxy_default_cost_per_gb_isp: float = 1.50
    proxy_default_cost_per_gb_residential: float = 4.00
    proxy_default_cost_per_gb_mobile: float = 8.00
    # One request in N lets a provider back onto a site where it has been
    # passed over for blocks, so a site can move back DOWN the price list in
    # hours rather than waiting a week for its record to age out. 0 disables.
    proxy_provider_reprobe_every: int = 20
    # A domain learned to need a dearer exit TYPE (residential after a
    # datacenter block) tries the next cheaper configured type again every Nth
    # success. Same shape as proxy_direct_reprobe_every. 0 disables.
    proxy_type_reprobe_every: int = 10

    # --- Bandwidth budget -------------------------------------------------
    # THE cost control. Residential bandwidth is billed per GB, and a runaway
    # crawl overnight is exactly how a large unexpected bill happens. At 80%
    # of a cap we warn; at 100% we refuse further proxied requests outright.
    # A cap on OUR OWN residential bandwidth (billed per GB), checked
    # before every proxied fetch. Not a per-customer limit: it is one global
    # pool, so when it trips, proxied fetches fail for EVERYONE at once.
    #
    # Raised 7 Sep 2026. The sums, so the next person changing
    # these knows what they are spending: multiply the monthly cap by your
    # provider's per-GB rate. Actual use when raised was 173.9 MB
    # for the month — 1.7% — so this is headroom, not a bill.
    #
    # Sized for the work in hand: a large first-pass crawl is ~230 MB, which
    # was already 46% of the old daily cap in a single day.
    # A domain learned to need a proxy is tried DIRECT again on every Nth
    # request. The flag used to be permanent: one rate-limit or timeout, then a
    # success through a stealth rung's own exit, and the domain paid residential
    # bandwidth forever. On 25 Sep 2026 41 of 75 flagged domains had never been
    # blocked at all (nameplay.org: 627 successes, 0 blocks, every one proxied).
    proxy_direct_reprobe_every: int = 20
    proxy_daily_budget_mb: int = 2_000
    proxy_monthly_budget_mb: int = 40_000
    proxy_budget_warn_fraction: float = 0.8
    # Refuse any single PROXIED response larger than this, before it is
    # streamed in full. Stops one enormous asset eating the day's allowance.
    # Enforced at tiers 0 and 1 (the rungs that download files) as a TERMINAL
    # refusal the ladder honours, so a refused download is not re-fetched by a
    # browser rung above it. 0 disables.
    proxy_max_response_mb: int = 25

    # --- Search (07-orchestration.md s8) -----------------------------------
    # Rented SERP vendor behind an adapter. Unset means /v1/search returns a
    # clear 503 rather than an empty result set — "no results" and "search is
    # unavailable" are different answers and an agent acts on them differently.
    # The hosted MCP endpoint at /mcp: bearer API key, metered like REST. Off = stdio only.
    mcp_http_enabled: bool = True

    search_provider: str = Field(default="")
    search_api_key: str = Field(default="")
    # The ladder, in preference order. Each name is tried until one answers, so
    # a single engine blocking us is a slower search rather than no search.
    search_ladder: str = Field(default="searxng,duckduckgo")
    # A self-hosted SearXNG. Empty disables that rung.
    searxng_url: str = Field(default="")
    # Scrapingdog's Google Search API — bought Google results. Empty disables it.
    scrapingdog_key: str = Field(default="")
    # Google results pages with rankings and the AI Overview (engine/core/serp.py),
    # from DataForSEO's live SERP API. Both empty disables /v1/serp with a 503.
    dataforseo_login: str = Field(default="")
    dataforseo_password: str = Field(default="")
    # Consecutive failures before a rung is skipped, and for how long.
    search_breaker_failures: int = Field(default=2)
    search_breaker_seconds: int = Field(default=180)

    # --- Structured extraction model (04-extraction.md s4) -----------------
    # Structured markup answers most schemas for free. When it does not, a
    # model infers the rest — only when one is configured. The API key is the
    # SDK's own ANTHROPIC_API_KEY (no ENGINE_ prefix); these name the model and
    # bound what one page may cost.
    extract_model: str = Field(default="claude-haiku-4-5")
    # Constrained rather than free-form: an invalid effort used to travel all
    # the way to the model API and fail there, on a customer's request. These
    # are the SDK's own accepted values.
    extract_effort: Literal["low", "medium", "high", "xhigh", "max"] = Field(default="medium")
    openai_extract_model: str = Field(default="gpt-4o-mini")
    extract_max_chars: int = Field(default=60_000)
    extract_max_tokens: int = Field(default=8_192)

    # --- Documents (/v1/parse) --------------------------------------------
    parse_max_mb: int = 25
    parse_max_pages: int = 500

    # --- Limits -----------------------------------------------------------
    default_rate_limit_rpm: int = 60

    # Shared secret for the operator app's /internal API. Empty disables it.
    internal_token: str = Field(default="")

    # --- Licence compliance (AGPL section 13) ------------------------------
    # A network service must offer its Corresponding Source to remote users.
    # Served by /v1/source from config, so a fork that changes the repository
    # updates the offer by changing a variable rather than remembering to edit
    # a document.
    # The offer must name a repository that actually serves the source: this
    # is a licence obligation, not a link. It has to exist and be public
    # BEFORE the service is exposed to remote users, or the offer cannot be
    # honoured. A deployment that forks sets ENGINE_SOURCE_URL instead.
    source_url: str = Field(default="https://github.com/snoopscan/scraping-engine")
    # Where a person gets an account and a key. Every "no key" answer names
    # these, so an agent can tell the person exactly what to do instead of
    # handing them a placeholder. A self-hosted deployment points it at its own.
    account_url: str = Field(default="https://snoopscan.com")
    # Where this deployment's API answers, as published in /openapi.json's
    # `servers`. API directories (APIs.guru) need it; a self-hosted copy sets
    # its own.
    public_api_url: str = Field(default="https://api.snoopscan.com")
    # Set from the build (git SHA) so the offer names the exact version
    # running, which is what section 13 actually asks for.
    source_revision: str = Field(default="")

    # --- Observability ----------------------------------------------------
    log_level: str = Field(default="INFO")
    environment: str = Field(default="development")

    @property
    def asyncpg_dsn(self) -> str:
        """asyncpg wants postgresql:// (not postgresql+asyncpg://)."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")

    @property
    def residential_configured(self) -> bool:
        return bool(
            self.proxy_enabled
            and self.proxy_residential_host
            and self.proxy_residential_port
            and self.proxy_username
        )

    @property
    def datacenter_configured(self) -> bool:
        return bool(
            self.proxy_enabled
            and self.proxy_datacenter_host
            and self.proxy_datacenter_port
            and (self.proxy_datacenter_username or self.proxy_username)
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
