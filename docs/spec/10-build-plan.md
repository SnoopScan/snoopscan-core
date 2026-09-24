# 10 — Build Plan

Phased. Each phase ends with something working and useful, not a half-built layer.

---

## Phase 0 — Foundations

**Goal:** repo, CI, database, and a request that goes end to end at tier 0.

- Repo per `00-overview.md` layout. Python 3.12, `uv` or Poetry
- CI: lint (ruff), type check (mypy strict on `core/`), test (pytest), **licence gate** (section 6 below)
- Postgres schema from `02-data-model.md`, Alembic migrations
- FastAPI app with `/health`, `/ready`, `/metrics`
- API key auth middleware
- `POST /v1/scrape` at tier 0 only, no extraction — returns raw HTML
- Docker Compose: api, postgres, redis

**Done when:** `curl` against `/v1/scrape` returns a real page, and CI is green including the licence gate.

Do not skip the licence gate to "add it later". Retrofitting it after dependencies are embedded is how a forbidden package ends up shipped.

---

## Phase 1 — Core scrape

**Goal:** a genuinely useful single-page scraper.

- Tier 1 (curl_cffi) with pinned impersonation profile
- Escalation controller, tiers 0-1 only
- Block detection layers 1 and 2 (status, headers, signatures)
- Extraction: heuristic path (trafilatura), page classification, markdown output
- `domain_profiles` read and write, tier memory working
- Caching via `pages` and `maxAge`
- Politeness (Redis token bucket)
- Cost accounting object on every response
- Full `/v1/scrape` request schema honoured

**Done when:** scraping 100 assorted URLs returns clean markdown for the large majority, per-domain tier memory demonstrably works, and cost reporting is accurate.

**Checkpoint:** measure the tier-1 success rate across the real target set. If it clears 70%, the browser tier is genuinely deferrable. If it is well below, bring Phase 4 forward.

---

## Phase 2 — Crawl and jobs

**Goal:** multi-page work.

- Redis + RQ, queue separation, HTTP worker
- Job lifecycle, `jobs` table, status endpoints
- Crawl frontier: normalisation, dedup, claiming, reaper
- Sitemap discovery, link extraction, filtering
- Honeypot avoidance
- `POST /v1/crawl`, `GET /v1/crawl/{id}`, `/pages`, `DELETE`
- `POST /v1/map`
- `POST /v1/batch/scrape`
- Webhooks with signing and retry
- Retention sweeper

**Done when:** a 500-page crawl completes, terminates correctly, respects politeness, and survives a worker being killed mid-run.

---

## Phase 3 — MCP and lead-gen v1

**Goal:** the two consumers that justify the project.

MCP server (`08-mcp-server.md`): all tools, context discipline, guardrails, parity tests.

Lead-gen (`09-leadgen-pipeline.md`), API sources first:
- `directories`, `products`, `companies`, `contacts` tables
- Hacker News, Product Hunt, SaaSHub, PeerPush ingestion
- Product resolution and domain extraction
- Contact discovery: mailto, plain text, obfuscation, Cloudflare decode, JSON-LD
- Form detection, social links
- Free verification pass, then paid adapter
- Jurisdiction and subscriber-type classification
- Suppression list
- Segmented CSV export

**Done when:** an agent researches over MCP without blowing its context, and the pipeline produces a segmented, deduplicated lead list from the API-based directories.

This is the first phase with direct business value. Everything before it is infrastructure.

---

## Phase 4 — Browser tiers

**Goal:** the targets that need JavaScript or fight back.

- Patchright integration, browser pool, context isolation, recycling
- Browser worker container, memory caps, scale-to-zero
- Asset blocking, readiness heuristics
- Tier 2 and 3 in the escalation ladder
- Proxy layer: pool, per-domain scoring, cooldown, retirement, sticky sessions
- Geographic coherence
- Bandwidth budget enforcement
- `actions` support
- Screenshot format
- Camoufox for tier 3h where a WAF warrants it

**Done when:** JS-rendered targets scrape correctly, proxy costs are tracked accurately per domain, and bandwidth caps demonstrably stop a runaway job.

Do not start this before Phase 3. It is the most expensive phase to build and run, and Phase 1's checkpoint tells you how much of it you actually need.

---

## Phase 5 — Extraction quality

**Goal:** the differentiation.

- Structured extraction path: repeated-block detection for forums and listings
- Structure-preserving conversion: tables, code, nested lists, quote nesting
- Schema-constrained extraction with validation and retry (`/v1/extract`, `json` format)
- Block detection layers 3 and 4: statistical soft-block, plausibility scoring
- Extraction confidence scoring
- Boilerplate sweep with phrase list
- Change tracking and `page_versions`
- Fixture suite across all page types with F1 tracked in CI

**Done when:** forum and product extraction measurably beat the heuristic baseline on the fixture set, and structured output validates against supplied schemas.

---

## Phase 6 — Hardening

**Goal:** run it unattended.

- Full observability: metrics, tracing, structured logs, dashboards, alerts
- Directory health monitoring
- Domain profile decay
- Proxy health checks and usage rollup
- Weekly live smoke test
- Backup and restore, tested by actually restoring
- Runbook: common failures and their fixes
- Load test at expected peak
- Security review: credential handling, log redaction, JS execution gating

**Done when:** a week passes with no manual intervention and alerts fire correctly on induced faults.

---

## Deferred

Not in scope. Revisit only with a specific case:

- **CAPTCHA solving.** A target requiring it is a signal to reconsider scraping it
- **Credentialed access.** Legally and operationally a different product — see `11-compliance.md` section 4
- **GPU model-based extraction.** Design for it (pluggable router), build it when hard-page volume justifies the cost
- **Multi-tenancy.** The API key is the boundary for now
- **Rust or Go workers.** Only on a measured Python bottleneck. It will not be the bottleneck at this scale
- **Public API productisation.** Different problem: billing, docs, support, abuse handling

---

## Repository conventions

- Type hints everywhere; mypy strict on `core/`
- Pydantic v2 for every boundary
- No bare `except:`. Every caught exception logged with context
- No `print`. Structured logging only
- Config from environment via a single settings module. No config reads scattered through the code
- Async throughout the request path. No blocking calls in async functions — enforce with a linter rule
- One module, one responsibility. If a file exceeds ~400 lines, it is doing too much

---

## CI pipeline

```yaml
stages:
  - lint          # ruff check, ruff format --check
  - typecheck     # mypy --strict engine/core
  - licence       # see below — BLOCKING
  - test          # pytest, coverage floor 75% on core/
  - fixtures      # extraction + detection fixture suites
  - build         # docker images
```

### Licence gate

Implements constraint C2. **Blocking, not advisory.**

```bash
pip-licenses --format=json > licences.json
python tools/check_licences.py licences.json
```

`check_licences.py`:

- Allow: MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, MPL-2.0, PSF, Unlicense
- **Deny outright:** any AGPL, GPL, LGPL, SSPL, CC-BY-NC, BUSL, Elastic Licence, or "source available"
- Deny by name regardless of declared licence: `firecrawl`, `nodriver`, `zendriver`, `maxun`, `skyvern`, `browserless`, `rnet`
- Assert `trafilatura >= 1.8.0` explicitly — earlier versions are GPLv3+
- Unknown or missing licence metadata: **fail**, require manual review and an explicit allowlist entry with a comment explaining the check

A build that pulls a forbidden licence fails. It does not warn.

Run the gate on a schedule too, not only on change — a transitive dependency can relicense under you.

---

## Deployment

Single x86 VPS, 4-8 vCPU, 16-32GB RAM, NVMe.

```
docker compose:
  api            2 replicas behind nginx
  worker-http    2-4 replicas
  worker-browser 0-4 replicas, scale on fetch:browser queue depth
  postgres       with pgbouncer (transaction mode)
  redis
  scheduler
```

Browser workers scale to zero when idle. Bursty load plus always-on browser capacity is money burnt.

- TLS terminated at nginx. Only the API is public
- Postgres, Redis, `/metrics`, and the MCP HTTP endpoint bound to the internal network only
- Secrets from environment, injected at deploy. Never in the image, never in the repo
- Volumes for Postgres data and browser worker tmp

### Backups

- Postgres: `pg_dump` nightly to offsite object storage, 30-day retention
- WAL archiving for point-in-time recovery once the data matters
- **Test the restore.** A backup that has never been restored is a hypothesis

---

## Rough sequencing

Phases 0-3 are the critical path to business value. Phase 4 is the largest single chunk and its scope depends on the Phase 1 checkpoint. Phase 5 is where competitive advantage lives and can proceed in parallel with 4 if capacity allows. Phase 6 is continuous rather than a discrete block.

Order matters more than estimates: do not start Phase 4 before Phase 3 ships, because the checkpoint data from Phases 1-3 determines how much browser tier is actually needed.
