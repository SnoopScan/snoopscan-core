# 03 — Fetch Tiers and Escalation

Implements principle P1: cheapest tier that works.

## The ladder

| Tier | Name | Implementation | Relative cost | Handles |
|---|---|---|---|---|
| 0 | `http` | httpx | 1x | Static HTML, APIs, sitemaps, robots.txt |
| 1 | `impersonate` | curl_cffi | 1.2x | TLS/HTTP2 fingerprint checks, most WAF-lite sites |
| 2 | `browser` | Patchright | 40x | JS-rendered content, SPAs |
| 3 | `stealth` | Patchright + residential proxy | 120x | Cloudflare, behavioural checks |
| 3h | `stealth_hard` | Camoufox + residential proxy | 200x | Canvas/WebGL fingerprinting, DataDome |
| 4 | `mobile` | Camoufox + mobile proxy | 400x | Sites requiring mobile-network IPs |

Costs are order-of-magnitude, not measured. The point is that tier 3 is roughly a hundred times more expensive than tier 1, so escalating carelessly is how a scraping budget disappears.

**Tier 1 does most of the work.** In independent 2026 benchmarking, curl_cffi cleared 26 of 31 Cloudflare-protected targets — matching a 130MB patched Chromium at a fraction of the cost. Do not reach for a browser because a site "looks modern".

---

## 1. Common interface

Every tier implements the same protocol:

```python
class FetchResult:
    url: str                  # final URL after redirects
    status_code: int | None
    headers: dict[str, str]
    body: bytes
    content_type: str | None
    tier: str
    latency_ms: int
    bytes_transferred: int
    proxy_id: str | None
    proxy_type: str | None
    browser_ms: int
    error: str | None

class Fetcher(Protocol):
    name: str
    async def fetch(self, req: FetchRequest) -> FetchResult: ...
    async def healthcheck(self) -> bool: ...
```

`bytes_transferred` must be **measured**, not estimated from body length. It includes headers and all subresources the browser pulled. This is the proxy bill.

---

## 2. Tier 0 — plain HTTP

`httpx`, async, HTTP/2 enabled, no fingerprint work.

Use for:
- robots.txt, sitemap.xml
- Known-cooperative domains (`min_working_tier = 'http'`)
- JSON APIs
- Any request where we control the endpoint

Config: connect timeout 10s, read timeout from request budget, max 5 redirects, no automatic retry (escalation handles that).

Do not spoof a browser User-Agent at this tier. A real Chrome UA on a request with a Python TLS fingerprint is a **worse** signal than an honest one — the mismatch is exactly what fingerprint checks look for. Send an honest identifying UA with a contact URL.

---

## 3. Tier 1 — HTTP impersonation

`curl_cffi` (MIT). The workhorse.

```python
from curl_cffi.requests import AsyncSession

async with AsyncSession(impersonate="chrome") as s:
    r = await s.get(url, proxy=proxy_url, timeout=t)
```

### Fingerprint coherence

The whole point is that every observable signal agrees. `curl_cffi` handles TLS (JA3/JA4), HTTP/2 SETTINGS frame, and header ordering. Our job is to not break it:

- **Never override** `User-Agent`, `Accept`, `Accept-Encoding`, `Sec-CH-UA*` — the API rejects attempts (see `01-api-surface.md`)
- `Accept-Language` **must** agree with proxy geography. A German exit IP sending `en-US,en;q=0.9` only is an anomaly. Derive from `location.country` when not supplied
- Pin the impersonation target (e.g. `chrome124`) rather than floating `chrome`. Floating means our fingerprint changes when the library updates, silently changing block rates

### Version drift

Browser fingerprints go stale as Chrome ships. Handle it:

1. Pin an explicit impersonation profile in config
2. Track per-profile success rate in `fetch_log`
3. Alert when a profile's success rate drops more than 15 points week-over-week
4. Test a new profile against a fixed target set before promoting it

This is standing maintenance. Schedule a quarterly review; do not wait for a failure.

---

## 4. Tier 2 — headless browser

**Patchright** (Apache-2.0), a Playwright drop-in with the standard detection leaks patched.

Not `nodriver` or `zendriver` — both AGPL-3.0 and disqualified by C2, regardless of quality.

### Browser pooling

Launching Chromium costs 1-3 seconds. Do not launch per request.

```
BrowserPool
  ├── browser 1  ──▶ context ──▶ page   (one job)
  │              ──▶ context ──▶ page   (another job)
  └── browser 2  ──▶ ...
```

- Pool holds N browsers (N = available RAM / 1.5GB, floor 1)
- Each request gets a **fresh context**, never a shared one. Contexts isolate cookies, storage and cache — sharing them cross-contaminates sessions and leaks state between targets
- Contexts are cheap (~50ms). Browsers are not
- Recycle a browser after 100 pages or 30 minutes, whichever first. Chromium leaks memory over long sessions
- Hard memory cap per browser container. On breach, kill and relaunch — do not attempt graceful recovery

### Asset blocking

Default on (`blockAssets: true`). Route-level abort for images, media, fonts and stylesheets:

```python
await context.route("**/*", lambda route: (
    route.abort() if route.request.resource_type in
    {"image","media","font","stylesheet"} else route.continue_()
))
```

Cuts bandwidth 60-90%. On residential proxies at per-GB pricing this is the single largest cost lever.

Auto-disable when a screenshot is requested, or when a page fails to render meaningfully with assets blocked (some sites gate content behind CSS-driven visibility — detectable as very low extracted word count, retry once with assets on).

### Readiness

`networkidle` is unreliable on pages with polling or analytics beacons. Use in order:

1. `domcontentloaded`
2. Then wait for a content signal: either a selector from `actions`, or a stability heuristic — DOM node count unchanged across two 250ms samples
3. Then apply `waitFor` if set
4. Hard cap at the remaining time budget

---

## 5. Tier 3 — stealth

Patchright plus residential proxy, plus fingerprint hardening. For Camoufox-class targets (canvas/WebGL fingerprinting, DataDome), tier 3h uses **Camoufox** (MPL-2.0), which spoofs at the C++ level rather than by injecting JS — injected patches are themselves detectable.

Camoufox is slow (tens of seconds on a challenge). Reserve it for domains where `domain_profiles.detected_waf` justifies it.

### What tier 3 adds

- Residential or mobile proxy, geo-matched to `location`
- Randomised viewport within realistic ranges (never exactly 1920x1080 — the headless default is a signal)
- Realistic timezone and locale matching the proxy geography
- Human-plausible interaction before content access on domains flagged for behavioural checks: small mouse movements, a scroll, a brief dwell

Behavioural scoring is now live at the edge on major WAFs — pointer movement, focus changes and visibility are scored across a session. Pure fingerprint spoofing does not defeat it. Where a target scores behaviour, either interact plausibly or accept the failure.

### What we do not do

- No image/audio CAPTCHA solving or token injection. Browser modes default to one bounded, provider-detected checkbox attempt (`captchaHandling: "off"` disables it). An explicit `captchaCheckbox` action can require caller-specified content.
- No credential-based access. Out of scope entirely — see `11-compliance.md` section 4

---

## 6. Escalation controller

The decision engine. Owns the time budget and the tier sequence.

### Algorithm

```
fetch(request):
    profile  = load_domain_profile(host)
    if profile.circuit_open_until > now:
        return Error(BLOCKED, "circuit open")

    start = profile.min_working_tier   unless request.tier forced
    ladder = tiers_from(start)
    budget = request.timeout

    for tier in ladder:
        if budget < tier.min_time_ms: break
        t0 = now
        result = tier.fetch(request, budget)
        spent  = now - t0
        budget -= spent
        log_attempt(tier, result)

        verdict = validate(result)          # see 05-block-detection.md

        if verdict.ok:
            record_success(profile, tier)
            return result
        if verdict.reason == TARGET_ERROR:
            return result                   # genuine 404/500 — do not escalate
        if verdict.reason == ROBOTS_DENIED:
            return Error(ROBOTS_DENIED)
        record_block(profile, tier, verdict.signal)
        continue                            # escalate

    open_circuit_if_needed(profile)
    return Error(BLOCKED, tiers_attempted=ladder)
```

### Escalation triggers

Escalate on:
- 403, 429, 503
- `cf-mitigated` response header present
- Challenge page signature matched
- Soft-block: 200 with content far below the domain's statistical baseline
- Redirect to a known challenge host
- Empty or near-empty body where the domain normally returns content

Do **not** escalate on:
- 404, 410 — the page is not there. A browser will not conjure it
- 401 — authentication required. Out of scope
- 5xx that is clearly the origin failing (no challenge markers, consistent across tiers)
- Network timeouts on the first attempt — retry the **same** tier once before escalating

That last distinction matters. A flaky connection retried at tier 3 costs a hundred times more than retrying at tier 1 and fixes nothing.

### Budget division

`timeout` is total, not per tier. Divide so that the expensive tiers get enough time to be worth attempting:

- Tier 0/1: max 15s each
- Tier 2: max 40% of remaining budget
- Tier 3/3h: whatever remains, floor 20s

If remaining budget is below a tier's minimum viable time, stop and return `BLOCKED` rather than starting an attempt that will time out.

---

## 7. Domain profile updates

After every request:

**On success at tier T:**
- `success_count += 1`, `last_success_at = now`
- If `T < min_working_tier`, lower `min_working_tier` to T — the site got easier
- Update running `avg_content_length` / `stdev_content_length`

**On block at tier T:**
- `block_count += 1`, `last_block_at = now`
- If blocked at `min_working_tier`, raise it one step
- Record `detected_waf` if the signal identifies one

**Decay (scheduled, weekly):**
- For domains with no block in 30 days and `min_working_tier > http`, lower one step and let it re-prove
- Without this, a domain that dropped its WAF costs browser-tier money forever

**Circuit breaker:**
- Failure rate over 50% across the last 20 attempts within 5 minutes → set `circuit_open_until = now + 15 min`
- On reopen, allow a single probe request before resuming normal traffic

---

## 8. Politeness

Independent of tiers, enforced before any fetch.

- Per-domain minimum delay: `domain_profiles.politeness_delay_ms`, default 1000ms
- Per-domain max concurrency: default 2
- Both enforced by a Redis token bucket keyed on domain, so limits hold across all workers
- `Retry-After` on a 429 is obeyed exactly, and raises `politeness_delay_ms` for that domain
- robots.txt `Crawl-delay` is honoured when higher than our default

Politeness is not optional and not configurable below the floor. Hammering a target gets our IP ranges burnt and is the fastest route to a legal complaint.

---

## 9. Testing

Each tier needs:

- **Unit**: mocked transport, verify headers, proxy wiring, timeout handling
- **Fingerprint**: assert against a JA3/JA4 echo service that tier 1 produces the expected fingerprint for its pinned profile. This test catches library-update drift — it is the highest-value test in the suite
- **Escalation**: table-driven. Given a sequence of tier verdicts, assert the controller escalates, stops, or returns correctly. No network
- **Budget**: assert total elapsed never exceeds `timeout` regardless of tier path
- **Pool**: assert contexts are never reused; assert browsers recycle at threshold; assert a killed browser is replaced

Keep a fixed set of ~20 live targets spanning difficulty levels for a weekly smoke run. Track pass rate over time — a drop is early warning that a library or a WAF changed.
