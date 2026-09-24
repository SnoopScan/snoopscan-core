# 01 — API Surface

The contract. Freeze this before building anything behind it.

Option names deliberately match Firecrawl's public API where the concept is the same, so anything already written against Firecrawl migrates by changing a base URL. Where our behaviour is better, we add options rather than renaming existing ones.

## Conventions

- Base path: `/v1`
- All requests and responses are JSON, `Content-Type: application/json`
- Auth: `Authorization: Bearer <key>`. Keys live in the `api_keys` table (see `02-data-model.md`)
- Timestamps: RFC 3339 UTC, e.g. `2026-08-31T14:22:01Z`
- All durations in milliseconds unless the field name says otherwise
- Unknown fields in a request are rejected with 400, not ignored. Silent typo acceptance causes long debugging sessions

## Standard response envelope

Success:

```json
{
  "success": true,
  "data": { }
}
```

Failure:

```json
{
  "success": false,
  "error": {
    "code": "BLOCKED",
    "message": "Target returned a challenge page at all attempted tiers",
    "detail": {
      "tiers_attempted": ["http", "impersonate", "browser"],
      "final_signal": "cf_challenge_page"
    }
  }
}
```

### Error codes

| Code | HTTP | Meaning |
|---|---|---|
| `INVALID_REQUEST` | 400 | Schema violation, unknown field, bad URL |
| `UNAUTHORIZED` | 401 | Missing or invalid key |
| `RATE_LIMITED` | 429 | Caller exceeded their rate limit |
| `TIMEOUT` | 504 | Fetch exceeded `timeout` |
| `BLOCKED` | 502 | Target blocked us at every attempted tier |
| `FETCH_FAILED` | 502 | Network-level failure (DNS, TLS, connection refused) |
| `TARGET_ERROR` | 502 | Target returned 4xx/5xx. Actual status in `detail.status_code` |
| `EXTRACTION_FAILED` | 500 | Fetch succeeded, extraction produced nothing usable |
| `ROBOTS_DENIED` | 403 | robots.txt disallows and `respectRobots` is true |
| `PROXY_UNAVAILABLE` | 503 | An explicitly requested proxy could not be supplied. Nothing was fetched |
| `SEARCH_UNAVAILABLE` | 503 | No rung of the search ladder could answer, or none can honour a requested parameter |
| `JOB_NOT_FOUND` | 404 | Unknown job id |
| `FORBIDDEN_SCOPE` | 403 | The key is valid but lacks the scope this endpoint needs |
| `INSUFFICIENT_CREDITS` | 402 | The balance is empty. Nothing was fetched or charged |
| `ENGINE_REFUSED` | 503 | We declined to try. `detail.signal` says why — see below. The target was not contacted and nothing was charged |
| `SERP_UNAVAILABLE` | 503 | Google results pages are not enabled on this deployment, or the results source refused the call |
| `PLACES_UNAVAILABLE` | 503 | Places is not part of this deployment |
| `PLATFORMS_UNAVAILABLE` | 503 | Platform shortcuts are not part of this deployment |
| `COMPANY_UNAVAILABLE` | 503 | Company enrichment is not part of this deployment |
| `INTERNAL` | 500 | Unhandled. Must be logged with a trace id |

`ENGINE_REFUSED` is ours, never the target's, and `detail.signal` names which
refusal it was — they have different fixes:

| `signal` | Meaning | What to do |
|---|---|---|
| `circuit_open` | Repeated failures across the **site** tripped the breaker | Retry after `detail.minutes`, or read a cached copy with `maxAge` |
| `url_backoff_open` | **This one URL** has failed repeatedly and is backed off. The rest of the site is unaffected | Retry that URL after `detail.minutes`; other pages work now |
| `tier_ceiling_below_floor` | The page needs a more expensive fetch tier than your `maxTier` allows | Raise `maxTier`, or leave it unset |
| `no_tier_available` | The rung this request needs is not built on this deployment | A configuration matter on our side |

A failed **action** step is reported against the step by index and type, and
is one of two codes: `INVALID_REQUEST` when the step cannot be carried out as
written (a selector that will not parse), or `EXTRACTION_FAILED` when a
well-formed step went unanswered — the page may have changed, not finished
loading, or shown something else. A failed step is never retried on another
tier: it would fail identically and bill twice.

`PROXY_UNAVAILABLE` exists so that `proxy` can be a guarantee rather than a
preference. A caller naming a proxy type is making a statement about which IP the
target may see; answering that with a direct fetch exposes their own address to
the target, silently and after the fact. So an explicit request that cannot be
served refuses instead. `proxy: "auto"` is unaffected — there the proxy is our
optimisation, and degrading to direct is correct.

`TARGET_ERROR` is distinct from `BLOCKED` on purpose. A genuine 404 from the target is not a blocking event and must not trigger tier escalation or proxy retirement.

## Cost accounting object

Every response that performs a fetch includes a `cost` object. This is principle P4 from the overview made concrete.

```json
"cost": {
  "tier": "impersonate",
  "tiers_attempted": ["http", "impersonate"],
  "proxy_used": true,
  "proxy_type": "datacenter",
  "proxy_bytes": 48213,
  "browser_ms": 0,
  "extraction_path": "heuristic",
  "cached": false
}
```

- `tier` — the tier that ultimately succeeded
- `tiers_attempted` — every tier tried, in order. Reveals escalation waste
- `proxy_bytes` — bytes over the proxy. The dominant real cost. Must be measured, not estimated
- `browser_ms` — wall-clock time a browser was held. Zero for HTTP tiers
- `extraction_path` — `heuristic` or `structured` (see `04-extraction.md`)
- `cached` — true if served from cache, in which case all other fields are zero

**Failed requests carry no cost and are never counted against any quota.**

---

## POST /v1/scrape

Fetch and extract a single URL. Synchronous.

### Request

```json
{
  "url": "https://example.com/article",

  "formats": ["markdown"],

  "onlyMainContent": true,
  "includeTags": [],
  "excludeTags": [],

  "maxAge": 172800000,
  "storeInCache": true,

  "waitFor": 0,
  "timeout": 60000,
  "actions": [],

  "headers": {},
  "mobile": false,
  "location": { "country": "GB", "languages": ["en-GB"] },

  "proxy": "auto",
  "tier": "auto",

  "blockAssets": true,
  "removeBase64Images": true,

  "respectRobots": true,
  "parsers": ["pdf"]
}
```

### Field reference

**`url`** (required, string) — absolute http/https URL.

**`formats`** (array, default `["markdown"]`) — one or more of:

- `"markdown"` — cleaned markdown
- `"html"` — cleaned HTML after boilerplate removal
- `"rawHtml"` — unmodified response body
- `"links"` — all discovered links, absolute-resolved
- `"summary"` — short LLM-generated summary. Costs an LLM call
- `"screenshot"` — object form: `{"type":"screenshot","fullPage":false,"quality":80}`. Forces browser tier. Taken from the same country, device and exit as the page itself (see `location`, `mobile`)
- `"network"` — every request the page made while it loaded, plus the ad and analytics tags it **fired**. Forces browser tier; never served from cache (it is an observation of one visit). See "The network format" below
- `"json"` — object form: `{"type":"json","schema":{...},"prompt":"..."}`, or a named template: `{"type":"json","template":"product"}`. Schema-constrained structured extraction. See "Templates" below
- `"changeTracking"` — object form: `{"type":"changeTracking","modes":["git-diff"]}`. Requires `markdown` also requested

**`onlyMainContent`** (bool, default true) — strip nav, header, footer, sidebars, cookie banners.

**`includeTags` / `excludeTags`** (arrays of CSS selectors) — applied to the **original DOM before extraction**, not to extracted output. `excludeTags` wins where both match.

**`maxAge`** (int ms, default 172800000 = 48h) — serve from cache if the cached copy is younger. `0` forces a fresh fetch. Matching Firecrawl's default is deliberate.

**`storeInCache`** (bool, default true) — whether to write this result to cache.

**`waitFor`** (int ms, default 0) — additional wait after load, browser tiers only. Applied on top of the tier's own readiness heuristics.

**`timeout`** (int ms, default 60000) — total budget across all tier attempts, not per tier. The escalation controller divides this budget.

**`actions`** (array, default empty) — browser interaction sequence. Presence forces browser tier. See "Actions" below.

**`headers`** (object) — extra request headers. Cannot override fingerprint-critical headers (`User-Agent`, `Accept`, `Accept-Language`, `Accept-Encoding`, `Sec-CH-*`) — those are owned by the fetch tier and a mismatch is a detection signal. Attempts to set them return 400.

**`mobile`** (bool, default false) — use a mobile fingerprint and viewport.

**`location`** (object) — `country` (ISO 3166-1 alpha-2) and `languages` (BCP 47 array). Routes the request through a residential exit **in that country** and sets `Accept-Language`, locale and timezone to match. If `languages` is omitted it is derived from `country`.

A country is a promise about the IP the target sees, because the ad a page serves, the results a search ranks and the catalogue a store lists are chosen by the visitor's IP, not by `Accept-Language`. So a request with a `country` is proxied — and billed as proxied — even on a site that would answer direct, and if no exit in that country is available the request fails with `PROXY_UNAVAILABLE` rather than being answered from somewhere else. Pass `"proxy": "none"` to get the language without the exit.

> Changed 18 Sep 2026: a `country` now always selects an exit in that country. Previously `proxy: "auto"` did so only for domains already known to need a proxy.

**`proxy`** (string, default `"auto"`) — `"none"` | `"datacenter"` | `"residential"` | `"mobile"` | `"auto"`. `"auto"` lets the escalation controller decide.

**`tier`** (string, default `"auto"`) — force a starting tier: `"http"` | `"impersonate"` | `"browser"` | `"stealth"`. `"auto"` uses per-domain memory. Escalation still applies unless `escalate: false`.

**`blockAssets`** (bool, default true) — block images, fonts, media and stylesheets at browser tiers. Cuts proxy bandwidth by 60-90%. Automatically disabled when `screenshot` is requested.

**`respectRobots`** (bool, default true) — see `11-compliance.md`. Overriding requires a documented reason.

**`parsers`** (array, default `["pdf"]`) — content types to parse rather than return raw.

### The network format

Ask for `"network"` and the page is loaded in a real browser, with its images and scripts, through the same exit as the page, and every request it makes is recorded:

```json
"network": {
  "trackers": [
    {"vendor": "gtm",  "kind": "loaded", "id": "GTM-5ABC12",       "event": null,        "status": 200,  "delivery": "confirmed",   "failed": false, "count": 1},
    {"vendor": "meta", "kind": "loaded", "id": "1689016458013442", "event": null,        "status": 200,  "delivery": "confirmed",   "failed": false, "count": 1},
    {"vendor": "ga4",  "kind": "hit",    "id": "G-8XYZ3",          "event": "page_view", "status": null, "delivery": "unconfirmed", "failed": false, "count": 1}
  ],
  "requests": [
    {"url": "https://www.example.com/", "method": "GET", "type": "document", "status": 200, "failure": null}
  ],
  "total": 143,
  "truncated": false
}
```

**`trackers`** is the answer to "did the tag fire, with which ID?". `kind` separates the two things people conflate: `loaded` means the vendor's library was fetched (the tag is installed); `hit` means a collection request actually went out (the tag fired). Recognised: `ga4`, `gtm`, `google_ads`, `meta`, `tiktok`, `linkedin`, `microsoft_ads`.

`delivery` is a separate, weaker claim than firing, and has three values because two would lie:

- `confirmed` — the vendor answered 2xx/3xx
- `refused` — an error status, or a failure meaning it never arrived (blocked, connection refused, DNS). `failed` is true only here
- `unconfirmed` — it went out and no answer was observed. Analytics beacons are fire-and-forget and commonly end `ERR_ABORTED` whether or not the vendor received them, so this is **not** a fault — the hit existing proves the tag fired

Only the vendors' own collection endpoints are recognised. A site running server-side tagging on its own subdomain sends nothing a third party can see, and is reported as absent rather than guessed at. From an EU or UK exit, a site that honours consent law will often fire **nothing** until a visitor accepts cookies — that is the site working correctly, and worth knowing, not a fault.

**`requests`** is capped at 500; `total` is how many the page actually made and `truncated` says whether the list was cut. Request **bodies are never returned**: they are read only to find a pixel's ID and event (TikTok sends them there), because a pixel can carry hashed emails and a beacon can carry what a visitor typed.

**Timing.** The log is read after the window `load` event and a short quiet period (both bounded), because tag managers fire many tags on window load rather than when the HTML is ready. Expect a `network` request to take 15–25 seconds on a heavy retail page. Some sites fire a tag only on a fraction of visits — a sampled or A/B-tested pixel — so a single check that does not see a tag is evidence, not proof, that it is missing; monitoring repeats the check.

The log comes from the page's own load. A second load happens only for a `screenshot`, or when the rung that answered could not record traffic; `network` and `screenshot` together share that one load.

Requests we decline to save bandwidth (images, fonts, streamed video segments) appear with `failure: "declined_by_snoopscan"` and cost nothing, since they never leave the browser. Tracker hosts are never declined — a Meta pixel is an image — and every tracker request is kept in the log even past the 500 cap.

### Response

```json
{
  "success": true,
  "data": {
    "markdown": "# Title\n\nBody...",
    "html": "<article>...</article>",
    "rawHtml": null,
    "links": ["https://example.com/a"],
    "screenshot": null,
    "network": null,
    "json": null,
    "changeTracking": null,
    "metadata": {
      "title": "Title",
      "description": "...",
      "language": "en",
      "author": "...",
      "publishedAt": "2026-08-01T00:00:00Z",
      "sourceURL": "https://example.com/article",
      "url": "https://example.com/article",
      "statusCode": 200,
      "contentType": "text/html; charset=utf-8",
      "pageType": "article",
      "wordCount": 1420,
      "extractionConfidence": 0.94
    },
    "cost": { }
  }
}
```

`sourceURL` is what was requested; `url` is where we ended up after redirects.

`pageType` and `extractionConfidence` are ours, not in Firecrawl. `pageType` is one of `article` | `docs` | `forum` | `product` | `listing` | `unknown`. `extractionConfidence` is 0-1 — see `04-extraction.md` section 5. **Consumers should treat anything below 0.5 as suspect.**

### Actions

Executed in order at browser tier.

```json
"actions": [
  {"type": "wait", "milliseconds": 2000},
  {"type": "wait", "selector": "#results"},
  {"type": "click", "selector": ".load-more"},
  {"type": "write", "selector": "#search", "text": "query"},
  {"type": "press", "key": "Enter"},
  {"type": "scroll", "direction": "down", "amount": 3},
  {"type": "screenshot", "fullPage": true},
  {"type": "scrape"},
  {"type": "executeJavascript", "script": "return document.title"}
]
```

Results collect in `data.actions`:

```json
"actions": {
  "screenshots": ["data:image/png;base64,..."],
  "scrapes": [{"markdown": "...", "html": "..."}],
  "javascriptReturns": [{"type": "string", "value": "Title"}]
}
```

`executeJavascript` runs in page context. It is a security surface — see `11-compliance.md` section 5. Disabled by default for any externally-issued API key.

---

## POST /v1/crawl

Crawl a site. Asynchronous — returns a job id immediately.

### Request

```json
{
  "url": "https://example.com",

  "limit": 100,
  "maxDepth": 3,
  "maxConcurrency": 5,

  "includePaths": ["^/blog/.*"],
  "excludePaths": ["^/admin/.*"],

  "allowExternalLinks": false,
  "allowBackwardLinks": false,

  "ignoreSitemap": false,
  "ignoreQueryParameters": false,
  "deduplicateSimilarURLs": true,

  "delay": 0,
  "respectRobots": true,

  "scrapeOptions": {
    "formats": ["markdown"],
    "onlyMainContent": true
  },

  "webhook": {
    "url": "https://our-app/hooks/crawl",
    "events": ["completed", "failed", "page"],
    "headers": {}
  }
}
```

`includePaths` / `excludePaths` are regexes matched against the **path plus query**, not the full URL. Exclude wins.

`allowBackwardLinks` permits crawling to URLs above the starting path on the same host. `allowExternalLinks` permits other hosts — off by default, and turning it on with a high `limit` is how you accidentally crawl the internet.

`delay` is minimum milliseconds between requests to the same host, on top of the politeness floor in `07-orchestration.md`.

`deduplicateSimilarURLs` collapses URLs differing only in tracking parameters and trailing slashes.

`scrapeOptions` accepts every `/v1/scrape` field except `url`.

### Response

```json
{
  "success": true,
  "data": {
    "id": "crawl_01J8Z...",
    "status": "queued",
    "url": "https://example.com",
    "limit": 40,
    "limitRequested": 10000,
    "creditsPerPage": 1,
    "creditsMax": 40
  }
}
```

**The limit is checked against the balance before anything is queued.** A
crawl bills per page as it runs, so `limit` is a spending ceiling, not a
preference. A limit the balance covers is honoured as asked. One it does not
is **lowered** to what the balance buys, and the lowered figure is what the
worker receives — `limit` is the real ceiling, `limitRequested` is what was
asked for. A balance that buys no pages at all is `402 INSUFFICIENT_CREDITS`
with nothing queued, rather than a job that starts and dies part-done.

`creditsPerPage` quotes the **direct** rate. `proxy: "auto"` is the default
and usually resolves to a direct fetch, so it is quoted as one; an explicit
`residential` / `datacenter` / `mobile` is a guarantee of a proxy and is
quoted at the proxied rate. It is a floor either way: a page forced up to a
browser tier costs more, and which pages those are is not knowable in advance.

### GET /v1/crawl/{id}

```json
{
  "success": true,
  "data": {
    "id": "crawl_01J8Z...",
    "status": "running",
    "total": 87,
    "completed": 34,
    "failed": 2,
    "skipped": 6,
    "creditsUsed": 48,
    "cost": {
      "credits": 48,
      "proxy_bytes": 4821300,
      "browser_ms": 12400,
      "tier_breakdown": {"http": 20, "impersonate": 12, "browser": 2}
    },
    "startedAt": "2026-08-31T14:00:00Z",
    "completedAt": null,
    "next": "https://api.example.com/v1/crawl/crawl_01J8Z.../pages?cursor=eyJ...",
    "data": []
  }
}
```

Statuses: `queued` | `running` | `completed` | `failed` | `cancelled`.

`total` is the current known frontier size and **grows during a crawl** as links are discovered. Do not treat `completed/total` as a reliable progress fraction early on.

`creditsUsed` is money, not a page count: it is the sum actually charged, and equals `cost.credits`. It does **not** track `completed` — a cache hit is free and a browser rung costs more than tier 0, so 34 completed pages can bill anything from 0 upwards. The example above shows 34 pages costing 48 credits because two of them needed the browser tier.

Every discovered URL ends in exactly one of `completed`, `failed` or `skipped` (external host, excluded path, robots, depth), and the three sum to `total` once the job is terminal. A caller paying per page must be able to reconcile the bill against the buckets.

`next` is an absolute URL built from the request's own origin, as Firecrawl's is; a client built against Firecrawl follows it without reassembling a path.

### GET /v1/crawl/{id}/pages?cursor=&limit=

Cursor-paginated page results. Default limit 50, max 200. Crawl results are not returned inline in the status response — a 10,000-page crawl must not be a single JSON body.

```json
{
  "success": true,
  "data": {
    "pages": [
      {
        "id": "page_01J8Z...",
        "url": "https://example.com/article",
        "sourceURL": "https://example.com/article",
        "ok": true,
        "errorCode": null,
        "markdown": "# ...",
        "metadata": { "...": "the same object /v1/scrape returns" }
      }
    ],
    "next": "https://api.example.com/v1/crawl/crawl_01J8Z.../pages?cursor=eyJ..."
  }
}
```

`metadata` is the `/v1/scrape` metadata object, field for field — `sourceURL`, `url`, `title`, `author`, `publishedAt`, `contentType`, `pageType`, `wordCount`, `extractionConfidence` and the rest — so a caller merging scrape and crawl output reads the URL from `metadata.sourceURL` on both. The top-level `url` and `sourceURL` are the same values, kept for clients that already read them. Failed pages appear here with `ok: false` and again, with their signals, under `/errors`.

### DELETE /v1/crawl/{id}

Cancel. Returns final counts. Already-completed pages remain retrievable.

---

## POST /v1/map

Discover URLs on a site without fetching page bodies. Fast and cheap.

```json
{
  "url": "https://example.com",
  "search": "pricing",
  "limit": 5000,
  "includeSubdomains": false,
  "ignoreSitemap": false
}
```

Response:

```json
{
  "success": true,
  "data": {
    "links": [
      {"url": "https://example.com/pricing", "title": "Pricing", "source": "sitemap"}
    ],
    "estimate": {
      "pages": 412,
      "creditsPerPage": 1,
      "credits": 412,
      "basis": "direct fetch; a proxied or browser page costs more"
    },
    "cost": { }
  }
}
```

`source` is `sitemap` | `crawl` | `robots`. Sitemap-first, falling back to a shallow crawl. `search` filters by substring against URL and title.

`estimate` is what crawling these URLs would cost. Map is the cheap half of
the pair — it names URLs without fetching their bodies — so it is where the
expensive half gets quoted, and `pages` counts the links actually returned
after `search` and `limit` are applied. A floor, not a promise, for the same
reason `creditsPerPage` is on crawl: the rung a site forces is only known
once it is tried. Bytes are never quoted in advance — see `proxy_bytes` above,
which must be measured rather than estimated.

---

## POST /v1/batch/scrape

Many URLs, one job. Async, same lifecycle as crawl.

```json
{
  "urls": ["https://a.com/1", "https://b.com/2"],
  "maxConcurrency": 10,
  "scrapeOptions": { }
}
```

Returns a job id. Poll `GET /v1/batch/{id}` and `GET /v1/batch/{id}/pages`.

Max 10,000 URLs per batch. Partial failure does not fail the job — per-URL status appears in the pages response.

---

## POST /v1/extract

Structured extraction against a schema, across one or more URLs.

```json
{
  "urls": ["https://example.com/product/1"],
  "schema": {
    "type": "object",
    "properties": {
      "name": {"type": "string"},
      "price": {"type": "number"},
      "currency": {"type": "string"},
      "inStock": {"type": "boolean"}
    },
    "required": ["name"]
  },
  "prompt": "Extract product details",
  "scrapeOptions": { }
}
```

Response data is an array of `{url, data, confidence, error}`.

**Or name a template instead of writing a schema** — the same templates the
scrape `json` format takes (see *Templates* below; `GET /v1/templates` lists
them):

```json
{"urls": ["https://shop.example/product/123"], "template": "product"}
```

Give `schema` **or** `template`, never both: a template *is* a schema. Sending
both, or neither, is `400 INVALID_REQUEST`.

**Output is validated against the supplied JSON Schema before returning.** A value that does not conform is an error, not a passthrough. This is the difference between "the model said something" and "we extracted data" — see `04-extraction.md` section 4.

---

## POST /v1/search

Web search, optionally with page bodies.

```json
{
  "query": "site:example.com pricing",
  "limit": 10,
  "sources": ["web"],
  "location": {"country": "GB"},
  "scrapeOptions": null
}
```

Backed by a ladder of providers (`ENGINE_SEARCH_LADDER`, default `searxng,duckduckgo`), each rung tried in order until one answers. Both defaults are free, so an ordinary search costs no provider money; a paid rung is only used if one is configured, and falling back to it is logged as a cost event. Pinning `ENGINE_SEARCH_PROVIDER` overrides the ladder. If `scrapeOptions` is present each result is also scraped, which is a fan-out — enforce `limit` hard.

---

## POST /v1/products

A store's whole catalogue from the endpoint the platform itself publishes —
Shopify's `/products.json`, WooCommerce's Store API — normalised to one
`Product` shape. One request per page of up to 250 products, no key, no
browser, no crawl. `platform: null` with `source: "none"` means the site is not
a platform we know how to ask; use `/v1/crawl`.

```json
{ "url": "https://store.example.com", "limit": 500 }
```

```json
{
  "success": true,
  "data": {
    "platform": "shopify",
    "source": "api",
    "pages_fetched": 2,
    "total": 294,
    "products": [
      {
        "platform": "shopify", "id": "7891", "title": "Tree Runner",
        "url": "https://store.example.com/products/tree-runner", "handle": "tree-runner",
        "vendor": "Allbirds", "product_type": "Shoes",
        "price": "98.00", "compare_at_price": null, "currency": null, "available": true,
        "sku": "TR-01", "images": ["https://…"], "variants": [{"title": "US 9", "price": "98.00", "available": true}],
        "tags": ["mens"], "created_at": "…", "updated_at": "…"
      }
    ],
    "cost": { "tier": "http", "extras": { "platform_page": 2 } }
  }
}
```

Prices are the store's own strings, in the store's currency, never converted.
Shopify's public JSON does not state the currency; WooCommerce's does.
Billed `platform_page` per listing page fetched.

## POST /v1/posts

A site's posts from its own API — WordPress `wp-json`, Substack's archive,
Squarespace `?format=json`, Discourse `/latest.json`, **Mastodon** (`/@user`)
and **Bluesky** (`/profile/handle`) — or, for everything else (Ghost, Drupal,
Webflow, Framer, plain sites), its RSS/Atom feed. `source` says
which. Same request shape as `/v1/products`; `Post` carries `title`, `url`,
`published_at`, `updated_at`, `author`, `excerpt`, `content_html` when the API
supplies it, and `extra` for platform-specific facts (Substack's paywall
`audience`, Discourse's reply counts).

## Platform on every page

Every `metadata` object now carries `platform` — `shopify`, `woocommerce`,
`squarespace`, `magento`, `amazon`, `wordpress`, `substack`, `discourse`,
`ghost`, `drupal`, `mediawiki`, `webflow`, `framer`, `wix` or `null` — detected
from the page (Amazon from the hostname). A `/v1/scrape` of a Shopify,
WooCommerce or **Amazon product page** also returns `data.product`: for the
stores, the structured product from their JSON (billed as one extra
`platform_page`); for Amazon, read off the fetched page itself at no extra
charge, with `null` for anything Amazon did not show that client. **Amazon
withholds the buybox from the honest tier-0 client** — title, bullets, brand,
images and ASIN come back, price, availability and rating do not, in stock or
not — and shows all of it to the browser tier (measured: the same ASIN, price
and 180,256 ratings at `tier: "browser"`, nothing at `auto`). So a priceless
Amazon product page **escalates once to the browser tier for the price by
default**, billed as that browser fetch; `escalate: false` opts out and keeps it
cheap. `/v1/products` covers Shopify, WooCommerce, Squarespace commerce and
Magento 2 (detected passively, or by probing the storefront GraphQL endpoint
when no passive marker is present).
`/v1/map` adds the platform's own listing as a third source, `"source":
"platform"`, and reports `platform` at the top level.

## Monitors — watch pages for changes on a schedule

```
POST   /v1/monitor                 create
GET    /v1/monitor                 list this key's monitors
GET    /v1/monitor/{id}            one monitor, with its latest check
DELETE /v1/monitor/{id}            delete (its checks go with it)
POST   /v1/monitor/{id}/run        check now, synchronously
GET    /v1/monitor/{id}/checks     recent checks (?limit=, default 20)
```

```json
{ "name": "Pricing", "urls": ["https://example.com/pricing"], "intervalMinutes": 60,
  "goal": "Alert when a plan price changes", "webhook": "https://hooks.example.com/snoop" }
```

`intervalMinutes` is at least 5 and defaults to 60; up to 50 URLs per monitor.
Each check scrapes every URL with change tracking (`maxAge: 0` — a monitor
exists to look again) and records a page status in Firecrawl's vocabulary:
`same`, `changed`, `new` (first capture), `error`. A changed page carries the
`git-diff`, the previous capture time and line counts; an error carries the
code and the check continues with the other URLs. Checks are billed as the
scrapes they are, against the key that created the monitor.

The webhook fires `monitor.check.completed` — signed like every other webhook,
with the key's `webhook_secret` — **only when a check has something to say**:
a change, a first capture, or an error. A check where every page is `same` is
recorded and not announced. The schedule advances from the time a check
finishes, not from when it was due, so a backlog after downtime runs once, not
once per missed slot. `goal` is stored and returned; it does not yet steer the
comparison.

## Templates — a schema you do not have to write

Ask for a kind of page by name and get the same fields from every site:

```json
POST /v1/scrape
{"url": "https://shop.example/product/123", "formats": [{"type": "json", "template": "product"}]}
```

| Template | For | Fields |
|---|---|---|
| `product` | A shop listing: name, brand, price, currency, availability, rating. | availability, brand, condition, currency, description, image… |
| `article` | A news or blog page: headline, author, dates, section, publisher. | author, dateModified, datePublished, description, headline, image… |
| `jobPosting` | A vacancy: title, employer, location, type, salary, dates. | baseSalary, datePosted, description, employmentType, hiringOrganization, jobLocation… |
| `localBusiness` | A business page: name, phone, address, opening hours, rating. | addressCountry, addressLocality, email, name, openingHours, postalCode… |
| `event` | A listing with a date: name, start and end, location, organiser, price. | currency, description, endDate, location, name, organizer… |
| `recipe` | A recipe: ingredients, steps, timings, yield, rating. | author, calories, name, ratingValue, recipeIngredient, recipeInstructions… |

`GET /v1/templates` lists them at runtime, with every field.

**Why they cost nothing.** The field names are schema.org's, which is what sites already publish in their JSON-LD. The fields are read from the page's own markup, so no model runs and no tokens are billed — you pay for the fetch and nothing else. A model is only called if a template is left unanswered **and** you supplied your own key; without one, the fields we could not find come back absent rather than invented.

Give `schema` **or** `template`, never both — a template *is* a schema, and quietly merging them would return fields you did not ask for. A hand-written `schema` still works exactly as before, and `prompt` may be combined with either.

Templates are per **page type**, not per site. A per-site recipe breaks the day that site is redesigned and only ever covers the sites someone wrote; a page-type template works on every site publishing standard markup.

## POST /v1/serp — Google rankings and the AI Overview

A live Google results page for a keyword, in a country, on a device, with the AI Overview and the pages it cited. Pass `domain` and the answer also says **where that site ranks** and **whether the AI Overview cites it** — the question a rank tracker or an AI-visibility monitor asks on every check.

```json
POST /v1/serp
{
  "keyword": "best running shoes",
  "country": "us",
  "device": "mobile",
  "domain": "allbirds.com",
  "aiOverview": true
}
```

| Field | Type | Default | |
|---|---|---|---|
| `keyword` | string ≤700 | required | Search operators like `site:` cost the provider 5× |
| `country` | ISO 3166-1 alpha-2 | `us` | The main markets are built in; for anything else pass `locationCode` |
| `locationCode` | int | — | Google's geographic target ID, for a country outside the built-in list or a city |
| `language` | string | `en` | Google's interface language code |
| `device` | `desktop` \| `mobile` | `desktop` | |
| `depth` | int 1–100 | 10 | Results to read; above 10 may cost more |
| `aiOverview` | bool | `true` | Also fetch the AI Overview (doubles the call's price) |
| `domain` | host or URL | — | The site to locate; subdomains count as the same site |

```json
"data": {
  "keyword": "best running shoes",
  "country": "us",
  "locationCode": 2840,
  "device": "mobile",
  "organic": [
    {"position": 2, "rank": 1, "url": "https://www.example-reviews.com/best-shoes", "domain": "www.example-reviews.com", "title": "…", "description": "…"}
  ],
  "aiOverview": {
    "present": true,
    "text": "The best running shoes for most people are…",
    "references": [{"url": "https://www.allbirds.com/pages/running", "domain": "www.allbirds.com", "title": "…", "source": "Allbirds"}]
  },
  "domain": {"domain": "allbirds.com", "position": 4, "url": "https://shop.allbirds.com/…", "citedInAiOverview": true, "citations": [ … ]},
  "itemTypes": ["ai_overview", "organic", "people_also_ask"],
  "checkUrl": "https://www.google.com/search?…",
  "cost": {"extras": {"serp": 1, "serp_ai_overview": 1}}
}
```

`position` is the result's place on the whole page (ads, AI Overview and panels included); `rank` is its place among organic results. `aiOverview.present: false` means Google showed none for this query, country and device — not that one failed to load.

**Price:** 10 credits a call, plus 10 when `aiOverview` is on. **Unavailable** (`503 SERP_UNAVAILABLE`, nothing charged) when the deployment has no results provider configured, or the provider refuses the call.

## POST /v1/places/search

Business listings from Google Maps: one search, a typed row per place, and —
opt-in — each place's website and phone, and the contacts leadgen finds on that
website. Synchronous. The contract is public; the source behind it is on the
proprietary side of the split, so the open core answers `503 PLACES_UNAVAILABLE`
with `detail.reason = "places_unavailable"` and charges nothing.

### Request

```json
{
  "query": "coffee shops",
  "location": "Austin, TX",
  "limit": 20,
  "includeDetails": false,
  "enrich": false,
  "timeout": 120000
}
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `query` | string 2–200 | required | What a person would type into Maps |
| `location` | string ≤120 | none | Appended as `… in {location}`; omit to let Maps infer |
| `limit` | 1–60 | 20 | Rows returned from the first results page |
| `includeDetails` | bool | false | One browser fetch per place: full address, website, phone, review count |
| `enrich` | bool | false | Each website through leadgen: emails, contact page/form, social links. Needs `includeDetails` to have a website to enrich |
| `timeout` | ms 10000–300000 | 120000 | Whole request |

Unknown fields are rejected with 400, as everywhere.

### Response

```json
{
  "success": true,
  "data": {
    "query": "coffee shops",
    "location": "Austin, TX",
    "places": [
      {
        "feature_id": "0x8644b4fda2c12fd5:0x66b58cb4722b37b",
        "name": "Jo's Coffee – South Congress",
        "place_url": "https://www.google.com/maps/place/Jo%27s+Coffee+%E2%80%93+South+Congress/data=…",
        "latitude": 30.2510458,
        "longitude": -97.7493717,
        "rating": 4.4,
        "review_count": 1890,
        "category": "Coffee shop",
        "address": "1300 S Congress Ave, Austin, TX 78704, United States",
        "tagline": "Coffee & snacks in a vibrant space",
        "open_status": "Open · Closes 7 pm",
        "website": "https://www.joscoffee.com/south-congress-jos",
        "phone": "+1 512-852-2300",
        "contacts": {
          "emails": ["info@coffee.example"],
          "contact_page_url": null,
          "contact_form_url": null,
          "social_links": {"facebook": "…", "instagram": "…"},
          "status": "found"
        }
      }
    ],
    "search_pages": 1,
    "detail_fetches": 20,
    "enriched": 18,
    "cost": {"tier": "browser", "extras": {"places_search": 1, "places_detail": 20}}
  }
}
```

`feature_id` is Google's own identity for the place and is stable across
searches; it is the key to dedupe on and, later, the key the detail and review
endpoints take. `website`, `phone`, `review_count` and the full `address` are
present only with `includeDetails`; `contacts` only with `enrich`. A place whose
detail fetch failed keeps its list-page fields and is still returned.

### Cost

`places_search` per results page (5 by default) and `places_detail` per detail
panel fetched (2), both additive and both tunable from the desk like every other
key. Enrichment bills through the fetch keys leadgen already uses. A failed
request is not charged.

## Job lifecycle

```
queued ──▶ running ──▶ completed
   │          │
   │          ├──────▶ failed
   │          │
   └──────────┴──────▶ cancelled
```

Terminal states are immutable. A job in `completed` never reopens; a re-crawl is a new job.

Results are retained per `07-orchestration.md` section 7 and then purged.

## Webhooks

POST to the configured URL:

```json
{
  "event": "page",
  "jobId": "crawl_01J8Z...",
  "timestamp": "2026-08-31T14:05:00Z",
  "data": { }
}
```

Events: `started`, `page`, `completed`, `failed`.

Signed with HMAC-SHA256 over the raw body, secret per API key, in `X-Signature-256` as `sha256=<hex>`. Receivers must verify.

Retry on non-2xx: 6 attempts, exponential backoff from 1s to 5 minutes. After that the delivery is dead-lettered and logged. Webhook failure never fails the job.

## Rate limiting

Per API key, token bucket. Headers on every response:

```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 87
X-RateLimit-Reset: 1725112800
```

429 responses include `Retry-After` in seconds.

Internal keys get a high limit. This exists to stop a runaway loop, not to monetise.

## Health and metrics

- `GET /health` — liveness. No auth. Returns 200 with `{"status":"ok"}`
- `GET /ready` — readiness. Checks Postgres and Redis. Non-200 pulls the instance from rotation
- `GET /metrics` — Prometheus format. Bound to the internal interface only, never public

## Versioning

Path-versioned (`/v1`). Additive changes ship without a version bump. Removing or renaming a field, or changing a default, requires `/v2`. Defaults are part of the contract — `maxAge` silently changing would corrupt downstream caching assumptions.
