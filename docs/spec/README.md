# Web Scraping Engine — Specification Set

Build specification for an in-house web scraping, crawling and content-extraction engine with a REST API, an MCP server, and a lead-generation pipeline.

Written to be handed to a developer or coding agent as the source of truth. Read `00-overview.md` first.

## Documents

| # | File | Contents |
|---|---|---|
| 00 | `00-overview.md` | Architecture, hard constraints, design principles, repo layout, technology decisions |
| 01 | `01-api-surface.md` | REST API — endpoints, request/response schemas, errors, webhooks. **The contract** |
| 02 | `02-data-model.md` | PostgreSQL schema, indexes, retention |
| 03 | `03-fetch-tiers.md` | Tiered fetching, escalation controller, browser pooling, politeness |
| 04 | `04-extraction.md` | Page classification, extraction routing, structured extraction, confidence scoring |
| 05 | `05-block-detection.md` | Block and soft-failure detection, decoy content, honeypot avoidance |
| 06 | _(not published)_ | Proxy selection, scoring, rotation, retirement, cost tracking — proprietary |
| 07 | `07-orchestration.md` | Queue, workers, crawl frontier, caching, scheduling, observability |
| 08 | `08-mcp-server.md` | MCP tools, resources, context discipline, guardrails |
| 09 | `09-leadgen-pipeline.md` | Directory ingestion, contact discovery, verification, list assembly |
| 10 | `10-build-plan.md` | Phased build, CI pipeline, licence gate, deployment |
| 13 | `13-ai-visibility.md` | **Brief.** Citation tracking across ChatGPT, Perplexity, Gemini and AI Overviews |
| 11 | `11-compliance.md` | Scraping posture, outreach law, security requirements, checklists |

## Reading order

**Before writing any code:** 00, 01, 02. Specs 01 and 02 are the contract — freeze them before building behind them.

**Then by phase**, per `10-build-plan.md`:

- Phase 0-1 → 03, 04, 05
- Phase 2 → 07
- Phase 3 → 08, 09, 11
- Phase 4 → 03 (browser sections), 06
- Phase 5 → 04, 05 (advanced sections)

## Three things that are not negotiable

**1. Clean-room implementation.** No code from Firecrawl or any AGPL/GPL/SSPL/source-available project. The API deliberately mirrors Firecrawl's option naming — interface compatibility is legitimate and gives free migration value — but the implementation is independent. Do not read Firecrawl source while writing the corresponding component.

**2. The licence gate is blocking.** CI fails, not warns, on a forbidden licence. Details in `00-overview.md` constraint C2 and `10-build-plan.md` section 6. Build it in Phase 0; retrofitting it later is how a forbidden package ships.

**3. No personal or identifying data in the repo.** No names, locations, company identifiers or credentials in source, comments, commits, fixtures or docs. Configuration from environment variables only.

## Design principles in one line each

- **P1** — Cheapest fetch tier that works. Tier 3 costs ~100x tier 1
- **P2** — Never trust HTTP 200. Validate every fetch before treating it as success
- **P3** — Extraction quality is the product. Route by page type
- **P4** — Stateless workers, stateful Postgres
- **P5** — Rent the IP layer, build the intelligence layer
- **P6** — Every component independently testable

## Where the value is

Two things differentiate this from what already exists:

1. **Extraction quality on non-article pages.** Heuristic extractors score ~0.55 F1 on forums and ~0.68 on product pages. Repeated-block detection and structure-preserving conversion (`04-extraction.md` section 4) is the highest-value work in the spec.

2. **Honest cost accounting.** Every response reports which tier ran, proxy bytes consumed, browser time held. Failed fetches are never counted. Competitors hide cost in credit multipliers; this is both an internal control and a differentiator.

## What is deliberately not built

CAPTCHA solving, credentialed access, GPU model-based extraction, multi-tenancy, Rust/Go workers, public API productisation. Reasons in `10-build-plan.md` "Deferred" and `11-compliance.md` section 4.
