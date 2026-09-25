# SnoopScan

Turn any public page into clean markdown, structured JSON, or a schema you
define — including the pages that block everything else. REST API, MCP server,
and SDKs.

This repository is SnoopScan's open core. It runs on its own, and the hosted
API at [snoopscan.com](https://snoopscan.com) adds the parts that are not here.
See [Open source vs hosted API](#open-source-vs-hosted-api).

## Quick start

The examples below call the hosted API and read your key from
`SNOOPSCAN_API_KEY`. The free plan includes 1,500 credits a month.

### curl

```bash
curl -X POST https://api.snoopscan.com/v1/scrape \
  -H "Authorization: Bearer $SNOOPSCAN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/", "formats": ["markdown", "links"]}'
```

### Python

```bash
pip install snoopscan
```

```python
from snoopscan import SnoopScan

snoop = SnoopScan(api_key="...")
print(snoop.scrape("https://example.com/").markdown)
```

### JavaScript / TypeScript

```bash
npm install snoopscan
```

```ts
import { SnoopScan } from 'snoopscan';

const snoop = new SnoopScan({ apiKey: '...' });
console.log((await snoop.scrape('https://example.com/')).markdown);
```

## Features

- Six fetch tiers on the hosted API, from a plain HTTP request up to a stealth
  browser on a residential exit. The engine starts at the cheapest tier a
  domain has historically needed and moves up only when it has to.
- Four layers of block detection, including a statistical comparison against
  the domain's own baseline. A 200 that carries a challenge page, a consent
  wall or a generated decoy counts as a failure.
- Extraction is routed by page type. Each page is classified first. Articles
  go through the heuristic path. Forums, listings, products and tables go
  through the structured path. Confidence is scored from 0 to 1 and reported.
- Shopify stores, WordPress blogs and Substacks are read from their own public
  JSON, so you get the content in one request instead of a crawl.
- Failed requests cost nothing. Every response reports the tier that
  succeeded and what it consumed.
- Every endpoint is also an MCP tool.

## Open source vs hosted API

This repository contains:

- the REST API
- extraction
- block detection
- the crawl frontier
- the MCP server
- the first two fetch tiers (plain HTTP and browser-grade TLS)
- storage
- both SDKs

These parts are not in this repository and run only on the hosted API at
[snoopscan.com](https://snoopscan.com):

- the proxy layer
- the browser and stealth tiers
- the anti-bot knowledge base
- the lead-gen pipeline

A self-hosted copy fetches with the first two tiers. A page that needs a real
browser or a residential exit comes back as blocked. It is reported as
blocked, never as a false success. The hosted API uses all six tiers, and its
free plan includes 1,500 credits a month.

## MCP server

Every endpoint is also an MCP tool, so an agent can call them directly with no
glue code.

```bash
claude mcp add --transport http snoopscan https://api.snoopscan.com/mcp \
  --header "Authorization: Bearer $SNOOPSCAN_API_KEY"
```

Apps that sign in instead of taking a key (the Claude app, ChatGPT) connect to
`https://api.snoopscan.com/mcp-oauth` and sign in with a SnoopScan account.

Any MCP client that speaks streamable HTTP connects the same way, including
Cursor, VS Code, Windsurf, Zed and Codex.

### Tools

`scrape`, `fetchMore`, `crawl`, `crawlStatus`, `crawlPages`, `map`, `search`,
`extract`, `checkChanges`, `listProducts`, `listPosts`, `domain`, `company`,
`findContacts`, `hiring`, `people`, `findLeads` and `leadsStatus`.

The company, contact, people, hiring and lead tools need the hosted API. This
repository answers them with a plain "not available on this deployment".

Every tool has a title and read-only / destructive labels. None of them
submits, posts or buys anything.

### Limits

The tools are designed to keep an agent's context small:

- Every content tool takes a `maxChars` budget.
- Truncation is always visible and returns a continuation token.
- Crawl tools never inline page bodies.
- Guardrails cap pages per session, concurrent crawls, crawl size and
  bandwidth.
- Every refusal explains the limit, so an agent can adapt instead of retrying
  blindly.
- `executeJavascript` is not exposed over MCP at any level.

## API endpoints

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

Every page includes `metadata.platform`, which says what built it. Every
response reports the tier that succeeded, the tiers attempted, and the
extraction path.

### Extract data with a schema

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

### Monitor a page for changes

This checks the page every 60 minutes and sends a webhook when it changes.

```bash
curl -X POST https://api.snoopscan.com/v1/monitor \
  -H "Authorization: Bearer $SNOOPSCAN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.com/pricing"],
       "everyMinutes": 60,
       "webhookUrl": "https://your.app/hook"}'
```

## Development

Set up and run the engine locally:

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
uv pip install -e sdk/python --python .venv/bin/python
cp .env.example .env && createdb scraping_engine && .venv/bin/alembic upgrade head
.venv/bin/python tools/create_key.py "local-dev" --rpm 600
.venv/bin/uvicorn engine.api.app:app --reload --port 8099
```

The JS/TS SDK is in `sdk/js` and builds on its own:

```bash
cd sdk/js && npm install && npm run build && npm test
```

Run the tests, lint and type checks:

```bash
.venv/bin/pytest engine/tests -q
.venv/bin/ruff check engine tools && .venv/bin/mypy --strict engine/core
.venv/bin/python tools/smoke.py          # live, not part of CI
```

The engine is built to the specs in `docs/spec/`. Spec 01 (API surface) and
spec 02 (data model) are the contract. Everything else is implemented behind
them.

### Dependency licences

Every dependency must be MIT, Apache-2.0, BSD, ISC or MPL-2.0. The check is
blocking: a build that pulls in any other licence fails instead of warning. It
also runs weekly, because a transitive dependency can change its licence
without you noticing.

```bash
.venv/bin/pip-licenses --format=json > licences.json \
  && .venv/bin/python tools/check_licences.py licences.json
```

Two results of this rule are already in the code. `psycopg2` (LGPL) is not
used, and Alembic migrates through asyncpg instead. `tld`, which comes in as a
transitive dependency, has a recorded allowlist entry that names which branch
of its tri-licence we use.

## Contributing

Contributions must follow three rules:

1. Clean-room implementation. Do not use code from any AGPL, GPL or SSPL
   project. The API mirrors the option names common in this category, since
   interface compatibility is legitimate, but the implementation is
   independent.
2. The licence check is blocking, not advisory.
3. No personal or identifying data in the repo. Configuration comes from
   environment variables only. Every fixture uses `example.com` or `.invalid`.

## Licence

The engine is licensed under AGPL-3.0. If you run a modified version as a
network service, section 13 requires you to offer its source to your users.
The running instance does this itself at `GET /v1/source`, so it does not
depend on a document that a fork might forget to update.

Both SDKs, Python and JS/TS, are MIT. An AGPL client library would push the
copyleft into every application that imports it, which is not what a client
library is for.

Some modules are proprietary and are not covered by the AGPL: the proxy layer,
the browser and stealth tiers, the anti-bot knowledge base, and the lead-gen
pipeline. They are listed in `LICENSE-PROPRIETARY`, which is generated from
`engine/licensing.py`. CI enforces the split: `tools/check_split.py` fails the
build if any public module depends on one of them.
