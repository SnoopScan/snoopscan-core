# 00 — Overview and Architectural Principles

## What this is

An in-house web scraping, crawling and content-extraction engine. It converts URLs into clean structured content (markdown + JSON), crawls sites, discovers URLs, and exposes all of it over a REST API and an MCP server.

It is built for two consumers:

1. **Internal content pipeline** — an LLM agent connects over MCP, researches, and writes results into Postgres.
2. **Internal lead-generation pipeline** — scheduled crawls of product directories, followed by contact discovery on each product's website, producing deduplicated outreach lists.

A third consumer is possible later (external customers, as a paid API). Every design decision in these specs assumes that possibility without building for it yet.

## Hard constraints

### C1 — Clean-room implementation

This project takes **no code** from Firecrawl or any other AGPL/GPL/SSPL/source-available project. The API surface deliberately mirrors Firecrawl's option naming because interface compatibility is legitimate and gives free migration value. The implementation is independent and original.

Do not read Firecrawl source while writing implementation code for the corresponding component. Work from the public API documentation and from these specs.

### C2 — License gate (build-breaking)

Every dependency must be MIT, Apache-2.0, BSD (2/3-clause), ISC, or MPL-2.0. No exceptions without written sign-off.

**Explicitly forbidden:**

| Package | License | Notes |
|---|---|---|
| firecrawl | AGPL-3.0 | The thing we are replacing |
| nodriver | AGPL-3.0 | Good tool, fatal license |
| zendriver | AGPL-3.0 | nodriver fork |
| maxun | AGPL-3.0 | |
| skyvern | AGPL-3.0 | |
| browserless | SSPL-1.0 | |
| rnet | GPL-3.0 | Repo metadata conflicts; treat as GPL |
| ReaderLM-v2 | CC-BY-NC-4.0 | Non-commercial only |
| trafilatura < 1.8.0 | GPLv3+ | **Pin >= 1.8.0, which is Apache-2.0** |

**Conditional:**

- `crawl4ai` — Apache-2.0 with an additional attribution clause in the LICENSE file. Read the file, not the badge. Attribution required if used.
- `changedetection.io` — Apache-2.0 with a clause restricting resale as a hosted service. Fine as internal reference, do not vendor.

**CI must enforce this.** See `10-build-plan.md` section 6 for the licence-check job. A build that pulls in a forbidden licence fails, it does not warn.

### C3 — No personal or identifying data in the repo

No names, locations, company identifiers, or credentials in source, comments, commit messages, fixtures or docs. Configuration comes from environment variables only. This keeps the repo portable if it is ever extracted into a product.

**This is a rule about the repository, not about the product.** It constrains what gets committed. It places no limit on what a caller may scrape or what the API returns — `/v1/scrape` returns names, email addresses and postal addresses exactly as they appear on the page, and is meant to. Read as a product constraint it would make the engine worse than the alternatives at the use case it exists for.

### C4 — Cost honesty

Every response reports what it actually cost to produce: which fetch tier was used, whether a proxy was consumed, how many bytes of proxy bandwidth, how long a browser was held. This is an internal cost-control requirement first and a competitive differentiator second. Failed fetches are never billed and never counted.

## Design principles

### P1 — Cheapest tier that works

Fetching escalates: plain HTTP, then impersonating HTTP, then headless browser, then stealth browser, then stealth browser on a residential proxy. Most pages resolve at tier 0 or 1. Never start at a higher tier than a domain has historically required.

The system remembers per-domain difficulty and starts at the tier that last succeeded for that domain.

### P2 — Never trust HTTP 200

A 200 response with a challenge page, a cookie wall, a consent gate, or generated decoy content is a failure. Every fetch passes through validation before it is treated as success. This is a first-class component, not an afterthought — see `05-block-detection.md`.

### P3 — Extraction quality is the product

Heuristic extraction is good on articles and poor on forums, product listings, and tables. The extraction layer routes by detected page type and uses a structure-aware path for the hard types. See `04-extraction.md`.

### P4 — Stateless workers, stateful Postgres

Workers hold no durable state. Postgres is the source of truth for jobs, content, cache, and proxy scores. Redis holds only the hot queue. Any worker can die at any point without data loss.

### P5 — Rent the IP layer, build the intelligence layer

Never build a proxy pool. Buy residential and datacenter bandwidth from a vendor. Do build the scoring, rotation, retirement and cost-attribution logic that sits on top of it — that part is ours.

### P6 — Every component independently testable

The fetch tier, extraction layer, block detector and proxy scorer each have a defined interface and can be exercised in isolation with fixtures. No component reaches into another's internals.

## System shape

```
                    ┌──────────────┐
   LLM agent ──────▶│  MCP server  │──┐
                    └──────────────┘  │
                                      ▼
   HTTP client ────────────────▶ ┌──────────┐
                                 │ REST API │
                                 └────┬─────┘
                                      │ enqueue
                                      ▼
                              ┌───────────────┐
                              │  Redis queue  │
                              └───────┬───────┘
                                      │
                     ┌────────────────┼────────────────┐
                     ▼                ▼                ▼
                ┌─────────┐     ┌─────────┐     ┌─────────┐
                │ worker  │     │ worker  │     │ worker  │
                └────┬────┘     └────┬────┘     └────┬────┘
                     │               │               │
                     └───────────────┼───────────────┘
                                     ▼
              ┌──────────────────────────────────────────┐
              │  fetch tiers → validate → extract        │
              └──────────────────┬───────────────────────┘
                                 ▼
                          ┌─────────────┐
                          │  Postgres   │
                          └─────────────┘
```

Browser-tier workers run in separate containers from HTTP-tier workers. They have different resource profiles (a browser worker needs 4GB+; an HTTP worker needs ~256MB) and must scale independently.

## Repository layout

Single repository, multiple deployable services.

```
/engine
  /api            FastAPI app — REST surface
  /mcp            MCP server — thin wrapper over the same internals
  /core
    /fetch        tier implementations + escalation controller
    /extract      extraction router + extractors
    /detect       block and soft-failure detection
    /proxy        proxy pool, scoring, rotation
    /frontier     URL discovery, dedup, politeness
  /workers        queue consumers (http worker, browser worker)
  /storage        Postgres access layer, migrations
  /leadgen        directory ingestion + contact discovery
  /tests
    /fixtures     saved HTML for extraction and detection tests
```

Rationale for one repo: the API, MCP server and workers all share the core. Splitting them means versioning an internal contract across repos for no benefit at this size. Split later if a component needs independent release cadence.

## Technology decisions

| Layer | Choice | Reason |
|---|---|---|
| Language | Python 3.12 | Every library we need is Python-first: curl_cffi, trafilatura, Playwright/Patchright, Camoufox |
| API framework | FastAPI | Async, native Pydantic schemas, generates OpenAPI free |
| Validation | Pydantic v2 | Request/response schemas are the contract; enforce them |
| Queue | Redis + RQ | Simplest thing that works for bursty load. Not Celery — its config surface is unjustified at this scale |
| Database | PostgreSQL 15+ | Already running. Source of truth |
| HTTP fetch | httpx (tier 0), curl_cffi (tier 1) | |
| Browser | Patchright (tier 2/3), Camoufox (tier 3 hard) | Both permissively licensed |
| Extraction | trafilatura >= 1.8.0, plus structure-aware path | |
| Observability | OpenTelemetry + Prometheus | Both Apache-2.0. Grafana deployed separately, never vendored |

**Do not go polyglot yet.** A Rust or Go fetch worker is justified only if Python throughput is measured as the bottleneck. It will not be at v0 or v1 — proxy bandwidth and target rate limits bind first.

## Deployment target

Single x86 VPS to start. 4-8 vCPU, 16-32GB RAM, NVMe. Postgres and Redis co-resident with API and HTTP workers. Browser workers in their own containers with hard memory caps.

Browser workers scale to zero between bursts. The workload is bursty by nature — do not pay for idle browser capacity.

## Reading order

1. `00-overview.md` — this file
2. `01-api-surface.md` — the contract everything else implements
3. `02-data-model.md` — Postgres schema
4. `03-fetch-tiers.md` — fetching and escalation
5. `04-extraction.md` — content extraction
6. `05-block-detection.md` — validation and soft-failure detection
7. proxy management — proprietary; see LICENSE-PROPRIETARY
8. `07-orchestration.md` — queue, jobs, crawl frontier, caching
9. `08-mcp-server.md` — MCP tool definitions
10. `09-leadgen-pipeline.md` — directory ingestion and contact discovery
11. `10-build-plan.md` — phases, milestones, CI, deployment
12. `11-compliance.md` — legal and data-protection requirements

Specs 01 and 02 are the contract. Build those first and freeze them before starting on 03 onward.
