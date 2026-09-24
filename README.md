# SnoopScan

Turn any public page into clean markdown, structured JSON, or a schema you
define — including the pages that block everything else. REST API, MCP server,
and SDKs.

## Why

- **Gets the page.** Six fetch tiers on the hosted API, from a plain HTTP
  request up to a stealth browser on a residential exit (see below for what
  this repository includes). The engine starts at the cheapest tier a
  domain has historically needed and climbs only when it has to.
- **Knows when it failed.** A 200 carrying a challenge page, a consent wall or
  a generated decoy is a failure, not a success. Four layers of block
  detection, including statistical comparison against the domain's own
  baseline.
- **Extraction that is routed, not guessed.** Page type is classified first,
  then articles take the heuristic path while forums, listings, products and
  tables take the structured path. Confidence is scored 0–1 and reported.
- **Asks, rather than crawls, where it can.** A Shopify store, a WordPress
  blog or a Substack is served from its own public JSON — one request instead
  of a crawl.
- **You only pay for what worked.** Failed requests carry no cost, and every
  response reports the tier that succeeded and what it consumed.

## This repository and the hosted API

This is SnoopScan's open core: the REST API, extraction, block detection, the
crawl frontier, the MCP server, the first two fetch tiers (plain HTTP and
browser-grade TLS), storage, and both SDKs. It runs on its own.

Some parts are not here: the proxy layer, the browser and stealth tiers, the
anti-bot knowledge base, and the lead-gen pipeline. They run only on the hosted
API at [snoopscan.com](https://snoopscan.com). So a self-hosted copy fetches with
the first two tiers, and a page that needs a real browser or a residential exit
comes back as blocked (reported honestly, never as a false success). The hosted
API climbs all six tiers, and its free plan includes 1,500 credits a month.

## Quick start

```bash
curl -X POST https://api.snoopscan.com/v1/scrape \
  -H "Authorization: Bearer $SNOOPSCAN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/", "formats": ["markdown", "links"]}'
```

```python
from snoopscan import SnoopScan

snoop = SnoopScan(api_key="...")
print(snoop.scrape("https://example.com/").markdown)
```

```bash
pip install snoopscan
```

```ts
import { SnoopScan } from 'snoopscan';

const snoop = new SnoopScan({ apiKey: '...' });
console.log((await snoop.scrape('https://example.com/')).markdown);
```

```bash
npm install snoopscan
```

## MCP

Every endpoint is also an MCP tool, so an agent calls them directly with no
glue code.

```bash
claude mcp add --transport http snoopscan https://api.snoopscan.com/mcp \
  --header "Authorization: Bearer $SNOOPSCAN_API_KEY"
```

Apps that sign in rather than take a key (the Claude app, ChatGPT) connect to
`https://api.snoopscan.com/mcp-oauth` and sign in with a SnoopScan account.

Any MCP client works the same way — Cursor, VS Code, Windsurf, Zed, Codex, or
anything speaking streamable HTTP. Tools: `scrape`, `fetchMore`, `crawl`,
`crawlStatus`, `crawlPages`, `map`, `search`, `extract`, `checkChanges`,
`listProducts`, `listPosts`, `domain`, `company`, `findContacts`, `hiring`,
`people`, `findLeads` and `leadsStatus` (the company, contact, people, hiring and
lead tools need the hosted API; this repository answers them with a plain "not
available on this deployment"). Every tool carries a title and
read-only / destructive labels, and none of them submits, posts or buys
anything.

Context discipline is the governing constraint. Every content tool carries a
`maxChars` budget, truncation is always visible and returns a continuation
token, and crawl tools never inline page bodies. Guardrails cap pages per
session, concurrent crawls, crawl size and bandwidth, and every refusal
explains the limit so an agent adapts instead of retrying blindly.
`executeJavascript` is not exposed over MCP at any level.

## Endpoints

```bash
POST /v1/scrape      one URL -> markdown, html, links, screenshot, json
                     (+ `actions`: click, type, scroll, then read the result)
POST /v1/crawl       a site, with a frontier, robots and politeness
POST /v1/map         every URL on a site, ordered by relevance
POST /v1/search      the web, with the results scraped
POST /v1/extract     your JSON schema — or a named template — filled from the page
GET  /v1/templates   the named templates (product, article, jobPosting, …) and their fields
POST /v1/batch       many URLs, one job
POST /v1/parse       a PDF, DOCX or XLSX into text
POST /v1/monitor     watch URLs on a schedule, webhook on change
POST /v1/products    a Shopify or WooCommerce catalogue, from its own API
POST /v1/posts       WordPress, Substack, Squarespace or Discourse posts
POST /v1/company     enrich one company from its own site
POST /v1/domain      what a domain runs, and who it belongs to
POST /v1/places      Maps listings, details and enrichment
```

Extract a schema:

```bash
curl -X POST https://api.snoopscan.com/v1/extract \
  -H "Authorization: Bearer $SNOOPSCAN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "urls": ["https://example.com/product/1"],
        "schema": {"type": "object",
                   "properties": {"name":  {"type": "string"},
                                  "price": {"type": "string"}}}
      }'
```

Watch a page and get a webhook when it changes:

```bash
curl -X POST https://api.snoopscan.com/v1/monitor \
  -H "Authorization: Bearer $SNOOPSCAN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.com/pricing"],
       "everyMinutes": 60,
       "webhookUrl": "https://your.app/hook"}'
```

Every page carries `metadata.platform` saying what built it, and every response
reports the tier that succeeded, the tiers attempted, and the extraction path.

## Development

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
uv pip install -e sdk/python --python .venv/bin/python
cp .env.example .env && createdb scraping_engine && .venv/bin/alembic upgrade head
.venv/bin/python tools/create_key.py "local-dev" --rpm 600
.venv/bin/uvicorn engine.api.app:app --reload --port 8099
```

The JS/TS SDK lives in `sdk/js` and builds independently:

```bash
cd sdk/js && npm install && npm run build && npm test
```

```bash
.venv/bin/pytest engine/tests -q
.venv/bin/ruff check engine tools && .venv/bin/mypy --strict engine/core
.venv/bin/python tools/smoke.py          # live, not part of CI
```

Built to the specification set in `docs/spec/`. Specs 01 (API surface) and 02
(data model) are the contract; everything else implements behind them.

## The licence gate

Every dependency must be MIT, Apache-2.0, BSD, ISC or MPL-2.0. The gate is
**blocking** — a build pulling a forbidden licence fails rather than warns —
and it runs weekly, because a transitive dependency can relicense underneath
you.

```bash
.venv/bin/pip-licenses --format=json > licences.json \
  && .venv/bin/python tools/check_licences.py licences.json
```

Two consequences already visible here: `psycopg2` (LGPL) is not used — Alembic
migrates through asyncpg instead — and `tld`, pulled in transitively, carries a
recorded allowlist entry naming which branch of its tri-licence we elect.

## Licence

The engine is **AGPL-3.0**. Run a modified version as a network service and
section 13 obliges you to offer its source to your users — the running instance
does this itself at `GET /v1/source` rather than relying on a document a fork
would forget to update.

Both **SDKs are MIT**, deliberately — the Python one and the JS/TS one. An
AGPL client library would push the copyleft into every application importing
it, which is the opposite of what a client is for.

Some modules are proprietary and are not covered by the AGPL: the proxy layer,
the browser and stealth tiers, the anti-bot knowledge base, and the lead-gen
pipeline. They are listed in `LICENSE-PROPRIETARY`, generated from
`engine/licensing.py`, and the exclusion is enforced in CI — `tools/check_split.py`
fails the build if any public module depends on one.

## Three things that are not negotiable

1. **Clean-room implementation.** No code from any AGPL/GPL/SSPL project. The
   API deliberately mirrors the category's option naming — interface
   compatibility is legitimate — but the implementation is independent.
2. **The licence gate is blocking.** Not advisory.
3. **No personal or identifying data in the repo.** Configuration comes from
   environment variables only. Every fixture uses `example.com` or `.invalid`.
