# 07 — Orchestration

Queue, workers, crawl frontier, caching, scheduling.

## 1. Queue

**Redis + RQ.** Not Celery — its configuration surface and operational weight are unjustified at this scale, and RQ's model (a Python function on a queue) is enough.

Queues, consumed by different worker pools:

| Queue | Worker type | Concurrency |
|---|---|---|
| `fetch:http` | HTTP worker | High (50-200) |
| `fetch:browser` | Browser worker | Low (RAM-bound, 2-8) |
| `extract` | HTTP worker | Medium |
| `leadgen` | HTTP worker | Medium |
| `maintenance` | Any | 1-2 |

Separating `fetch:http` from `fetch:browser` is the important split. Browser jobs are memory-bound and slow; HTTP jobs are network-bound and fast. One pool for both means browser jobs starve HTTP throughput, or HTTP concurrency exhausts RAM.

Redis holds queue state only. If Redis is lost, in-flight jobs are recovered from Postgres — `frontier` rows in `claimed` state past their timeout return to `pending` (see `02-data-model.md` section 3). **Redis is a cache, not a system of record.**

### Job payloads

Payloads carry ids, never data. A queue message is `{job_id, frontier_id}` — the worker loads what it needs from Postgres. Keeps Redis small and means a payload cannot go stale.

### Priorities

RQ supports multiple queues consumed in order. Use three: `high` (interactive `/v1/scrape`), `default` (crawls, batches), `low` (maintenance, decay, sweeps). A synchronous API call must not queue behind a 10,000-page crawl.

---

## 2. Workers

### HTTP worker

Async, one process, high concurrency via asyncio. Memory ~256MB. Scales horizontally.

Handles tier 0 and 1 fetching, extraction, lead-gen tasks.

### Browser worker

One process holding a browser pool. Memory 4GB+ per container, hard-capped. Handles tier 2, 3, 3h.

**Scales to zero.** Between bursts there should be no browser worker running. Bursty load plus always-on browser capacity is pure waste. Scale up on queue depth for `fetch:browser`, scale down after an idle period.

### Lifecycle

- Graceful shutdown: stop claiming, finish in-flight, release claimed frontier rows, exit. Honour SIGTERM
- Heartbeat to Redis. A worker that stops heartbeating has its claims reaped
- Crash: claimed rows return to `pending` after timeout. Idempotency (section 6) makes re-execution safe

---

## 3. Crawl frontier

Postgres-backed, per `02-data-model.md` section 3.

### URL normalisation

Applied before hashing for dedup. Order matters:

1. Lowercase scheme and host
2. Strip default port (`:80` http, `:443` https)
3. Remove fragment
4. Sort query parameters alphabetically
5. Drop tracking parameters — `utm_*`, `fbclid`, `gclid`, `msclkid`, `ref`, `source`, and a maintained list
6. Strip trailing slash unless the path is root
7. Decode unnecessary percent-encoding
8. If `ignoreQueryParameters`, drop the query entirely

Keep the list of tracking parameters in a data file. It grows.

### Discovery

Sources, in order:

1. **Sitemap** — `/sitemap.xml`, sitemap index recursion, and any `Sitemap:` directive in robots.txt. Cheapest and most complete. Always try first unless `ignoreSitemap`
2. **Crawl** — links extracted from fetched pages
3. **robots.txt** — occasionally reveals paths

Sitemap-first is not just polite, it is faster and cheaper than discovering the same URLs by crawling.

### Filtering

Each discovered URL passes:

- Scheme is http/https
- Host matches policy (`allowExternalLinks`, `includeSubdomains`)
- Path passes `includePaths` / `excludePaths` (exclude wins)
- Depth within `maxDepth`
- Not a honeypot (see `05-block-detection.md` section 6)
- robots.txt permits, when `respectRobots`
- Not already in frontier (unique index handles this)
- Extension not in the skip list (`.zip`, `.exe`, `.dmg`, media, unless explicitly requested)

Skipped URLs insert with `status='skipped'` and a `skip_reason`. Auditable. Silent drops make crawl behaviour impossible to debug.

### Ordering

Breadth-first: `ORDER BY depth, id`. Shallow pages are usually the valuable ones, and depth-first crawls that wander into a calendar widget never terminate.

### Termination

A crawl ends when: no `pending` rows remain, or `limit` reached, or cancelled, or the whole-job timeout expires. Always terminate — an unbounded crawl is a bug, not a feature.

---

## 4. Politeness

Per-domain, enforced across all workers via Redis token bucket keyed on domain. Local-only rate limiting fails the moment there is more than one worker.

- Minimum delay from `domain_profiles.politeness_delay_ms`, default 1000ms
- Max concurrent from `domain_profiles.max_concurrency`, default 2
- `Retry-After` obeyed exactly and raises the stored delay
- robots.txt `Crawl-delay` honoured when higher than ours
- Floor is not configurable below the default

A worker that cannot acquire a token requeues the job with a short delay rather than blocking. Blocking a worker on a rate limit wastes a slot that could serve another domain.

---

## 5. Caching

The cache is the `pages` table. There is no separate cache store.

Lookup on any fetch with `maxAge > 0`: most recent successful row for this `normalized_hash` newer than `maxAge`. Hit returns immediately with `cost.cached = true` and all cost fields zero.

Rationale for one store: no invalidation problem, no divergence between cache and record, and cached content is queryable alongside everything else. The cost is that `pages` grows — handled by retention and partitioning (`02-data-model.md` section 10).

Default `maxAge` is 48 hours, matching Firecrawl, so behaviour is unsurprising to anything migrating.

---

## 6. Idempotency

Workers must be safe to re-run. A crashed worker's claim is reaped and the row re-executed.

- Page writes are upsert on `(job_id, normalized_hash)`, not blind insert
- Counter increments use `UPDATE ... SET completed = completed + 1` guarded so a re-run does not double-count — track completion on the frontier row, derive counters from it
- Webhook delivery is keyed by `(job_id, event, page_id)` and deduplicated. A crash between write and webhook must not send twice
- Proxy usage rows are keyed by attempt id

Deriving job counters from frontier state rather than incrementing them independently removes a whole class of drift bugs.

---

## 7. Retention

Per `02-data-model.md` section 10. Sweeper runs on the `maintenance` queue, hourly, batched:

```sql
DELETE FROM jobs WHERE expires_at < now() AND status = 'completed'
  AND id IN (SELECT id FROM jobs WHERE expires_at < now() LIMIT 1000);
```

Always `LIMIT`ed. An unbounded `DELETE` on a large table locks it and takes the API down with it.

`raw_html` nulled at 7 days while the row is kept — it is the dominant storage cost and rarely needed after the fact.

---

## 8. External services

### SERP

`/v1/search` walks a **ladder** of providers and takes the first rung that answers.

One provider is one point of failure, and search providers fail constantly. Measured
across a single afternoon on 4 September 2026: DuckDuckGo went from answering every
query to refusing 24 of 24; Mojeek from answering to blocking outright; Brave from
23 of 24 to HTTP 429 under nothing more than a benchmark's volume. A single-provider
endpoint was down three times in one afternoon.

The default ladder is `searxng,duckduckgo`. A self-hosted SearXNG leads it because it
spreads one query across many engines at once — so no single engine carries the load —
and because its per-engine parsers are maintained by a project for which that is the
whole job. Every engine-specific scraper we write instead is a thing that breaks when
that engine changes. Measured at the moment Brave was returning 429 to our own
scraper, a local SearXNG answered the same query with 38 results, 18 of them Brave's.

Rules the walk follows:

- **An empty result set is not an answer while rungs remain.** A blocked engine
  usually returns a page that parses to nothing, and asking the next rung is cheap.
- **If every working rung returns empty, that IS the answer**, returned as empty.
  An agent told "unavailable" retries; an agent told "no results" moves on, and only
  one of those is right when the web genuinely has nothing.
- **A rung that fails repeatedly is skipped** for `search_breaker_seconds`. It will
  refuse the next request too, and the caller should not pay for that guess.
- **`search_provider`, when set, pins one vendor and disables the walk.** An operator
  who names a vendor gets that vendor; falling through silently would make the setting
  a lie.
- **503 only when no rung could answer**, and the message names what was tried.

**Search parameters are requirements, not preferences.** `/v1/search` carries the
knobs a real caller turns — `place`, `language`, `device`, `freshness`, `safeSearch`,
`page`, `autoCorrect` — and providers differ wildly in which they support:
Scrapingdog does all of them, SearXNG does language/freshness/safeSearch/page,
DuckDuckGo's lite endpoint does a region and nothing else.

So each provider declares a `supports` set and the ladder holds it to it. A rung that
cannot honour a parameter the caller SET is **dropped from the walk**, not asked and
quietly allowed to answer with something else — a mobile SERP is a different page from
a desktop one, and serving desktop results under a mobile label is wrong in a way the
caller cannot detect. If no rung survives, that is a 503 naming the parameter, which is
the honest answer and tells the operator exactly which rung to add.

`supports` is per-parameter; an optional `honours(q)` covers the per-VALUE exceptions
(SearXNG does freshness, but its `time_range` starts at a day, so "the last hour" is
declined rather than silently widened). Defaults are not demands: `page=1` and
`autoCorrect=true` exclude nothing, so the common query stays on the free rungs.

A `search_health` canary runs quarter-hourly, asking each rung a question with a known
answer. Providers do not announce that they have started refusing you, and a thinning
ladder is the warning before the outage.

Interface each rung behind an adapter so the vendor stays swappable:

```python
class SerpProvider(Protocol):
    async def search(self, query: str, limit: int,
                     country: str | None) -> list[SearchResult]
```

Cache SERP results in `pages` keyed by a hash of `(query, country, limit)` with a short `maxAge` (1 hour default). Repeated identical searches during a research session are common and each one costs money.

### Email verification

Used by the lead-gen pipeline (`09-leadgen-pipeline.md`). Same adapter pattern. Never verify the same address twice within its re-verification window.

---

## 9. Scheduling

Recurring jobs on the `maintenance` queue:

| Task | Cadence | Purpose |
|---|---|---|
| Domain profile decay | Weekly | Lower `min_working_tier` on quiet domains (`03-fetch-tiers.md` s7) |
| Proxy health check | 15 min | proxy layer (not in the open core) |
| Proxy usage rollup | Daily | Aggregate then prune raw rows |
| Retention sweep | Hourly | Section 7 |
| Frontier reaper | 1 min | Return stale claims to pending |
| Directory ingestion | Daily | `09-leadgen-pipeline.md` |
| Re-verification sweep | Daily | Contacts past their window |
| Fixture smoke test | Weekly | ~20 live targets, track pass rate |

Use a simple scheduler (`rq-scheduler` or a cron container). Do not build one.

---

## 10. Observability

**Metrics** (Prometheus, Apache-2.0):

- `fetch_attempts_total{tier,outcome,domain}`
- `fetch_duration_seconds{tier}`
- `proxy_bytes_total{type,domain}`
- `queue_depth{queue}`
- `browser_pool_size`, `browser_pool_in_use`
- `extraction_confidence` histogram
- `block_rate{tier,signal}`
- `job_duration_seconds{kind}`

**Tracing** (OpenTelemetry, Apache-2.0): one span per request, child spans per tier attempt, extraction, and validation. Escalation paths are hard to debug from logs alone.

**Logging**: structured JSON, one line per event, always carrying `job_id` and `trace_id`. Never log credentials, proxy URLs with auth, or full page bodies.

Grafana runs as a separate deployment for dashboards. Its code is never vendored (AGPL).

### Alerts

| Condition | Severity |
|---|---|
| Block rate up >20 points week-over-week | Warning |
| Tier 1 block rate rising | Warning — likely fingerprint drift |
| Proxy bandwidth at 80% of cap | Warning |
| Proxy bandwidth at 100% | Critical |
| Queue depth growing 30+ min | Warning |
| Browser worker OOM kills | Warning |
| Extraction confidence mean dropping | Warning — possible regression |
| Postgres connections near limit | Critical |

---

## 11. Failure modes

| Failure | Behaviour |
|---|---|
| Redis down | API returns 503 for async endpoints; sync scrape still works; queued state recovered from Postgres on restart |
| Postgres down | Full outage. Fail fast and loudly — do not serve stale or partial results |
| Proxy vendor down | Fall back to secondary vendor; if none, degrade to direct where policy allows, else fail with a clear error |
| Browser worker crash | Claims reaped, job retried; browser relaunched |
| One SERP rung down | The ladder falls through; search answers from the next rung |
| Every SERP rung down | `/v1/search` returns 503 naming what was tried; everything else unaffected |
| Disk full | Critical alert. Sweeper should prevent it; monitor free space |

The Postgres case is deliberate. A scraping engine that silently serves stale data during a database outage is worse than one that stops.
