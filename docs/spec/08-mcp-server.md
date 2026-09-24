# 08 — MCP Server

Exposes the engine to LLM agents over the Model Context Protocol.

## Design position

The MCP server is a **thin adapter over the same core** as the REST API. It does not reimplement anything. If a behaviour differs between REST and MCP, that is a bug.

What it does add is agent ergonomics: tool descriptions written for a model rather than a developer, response shaping to fit a context window, and guardrails against an agent doing something expensive by accident.

Transport: stdio for local use, streamable HTTP at `/mcp` for remote. Both from the same implementation.

---

## 1. Context window discipline

The single most important design constraint, and where most MCP wrappers fail.

A 50,000-word page returned in full fills the agent's context and destroys the session. Rules:

- **Every content-returning tool has a `maxChars` parameter, defaulting to 20,000.** Content beyond it is truncated with an explicit marker and a continuation token
- **Crawl and batch tools never return page bodies inline.** They return a job id and a summary. Bodies are fetched per page, on demand
- **Truncation is always visible.** `"...[truncated: 42,000 of 68,000 chars. Use fetchMore with token 'abc' for the remainder]"`. Silent truncation makes an agent confidently wrong about content it never saw
- **Default to `markdown` only.** Never return `rawHtml` unless explicitly asked — it is enormous and mostly useless to a model

---

## 2. Tools

### `scrape`

> Fetch a single web page and return its content as clean markdown. Use this when you have a specific URL and need to read what is on it. For finding pages first, use `search` or `map`.

```json
{
  "url": "string, required",
  "formats": "array, default ['markdown']",
  "onlyMainContent": "bool, default true",
  "maxChars": "int, default 20000",
  "maxAge": "int ms, default 172800000",
  "waitFor": "int ms, default 0"
}
```

Returns markdown, key metadata (title, page type, word count), and a compact cost line. Omits the fields an agent has no use for.

### `search`

> Search the web and return results. Optionally fetch the content of each result. Fetching content is much slower and more expensive — only set `fetchContent` when you actually need the page bodies rather than just the links and snippets.

```json
{
  "query": "string, required",
  "limit": "int, default 5, max 20",
  "fetchContent": "bool, default false",
  "maxCharsPerResult": "int, default 5000"
}
```

The warning in the description is deliberate. `fetchContent: true` with `limit: 20` is 20 page fetches. Models will do this unless told not to.

### `map`

> List the URLs on a website without fetching page content. Fast and cheap. Use this to understand a site's structure before deciding what to scrape.

```json
{
  "url": "string, required",
  "search": "string, optional substring filter",
  "limit": "int, default 100, max 5000"
}
```

Returns URL and title only.

### `crawl`

> Start crawling a website. This runs in the background and returns a job id immediately. Use `crawlStatus` to check progress and `crawlPages` to read results. Crawls can take minutes and consume significant resources — set `limit` conservatively.

```json
{
  "url": "string, required",
  "limit": "int, default 20, max 500",
  "maxDepth": "int, default 2",
  "includePaths": "array, optional",
  "excludePaths": "array, optional"
}
```

Note the MCP defaults are far lower than the REST API's. An agent should not be able to start a 10,000-page crawl from a casual instruction. If a larger crawl is genuinely wanted, it goes through the REST API where a human set it up.

### `crawlStatus`

```json
{ "jobId": "string, required" }
```

Returns status, counts, cost. No page bodies.

### `crawlPages`

```json
{
  "jobId": "string, required",
  "cursor": "string, optional",
  "limit": "int, default 5, max 20",
  "maxCharsPerPage": "int, default 5000"
}
```

Paginated. Small defaults, on purpose.

### `extract`

> Extract structured data from one or more pages according to a JSON schema. The output is validated against the schema — if a page does not contain the required fields, that page returns an error rather than invented values.

```json
{
  "urls": "array, required, max 10",
  "schema": "JSON Schema object, required",
  "prompt": "string, optional"
}
```

The description states the validation guarantee because it changes how an agent should treat the output — it can rely on shape, and it should treat an error as genuine absence rather than retrying.

### `checkChanges`

> Check whether a page has changed since it was last fetched.

```json
{
  "url": "string, required",
  "includeDiff": "bool, default false"
}
```

Returns `new` | `changed` | `same`, previous fetch time, and optionally a diff.

### `findContacts`

> Find contact information for a company website: contact page, contact form, and any published email addresses. Used by the lead pipeline.

```json
{ "url": "string, required" }
```

Returns contact page URL, form presence and vendor, discovered emails with their source, and social links. See `09-leadgen-pipeline.md`.

---

## 3. Resources

Read-only data the agent can reference without a tool call.

| URI | Content |
|---|---|
| `engine://jobs/recent` | Recent jobs with status |
| `engine://domains/{domain}` | Domain profile: known difficulty, typical page type, politeness |
| `engine://stats/today` | Today's usage — pages, bandwidth, block rate |

The domain resource is genuinely useful to an agent: knowing a target is browser-tier-only lets it set expectations about latency before starting.

---

## 4. Prompts

Reusable templates exposed to the client.

- `research-topic` — search, select sources, scrape, synthesise
- `audit-site` — map, sample pages, report structure and content types
- `find-leads` — ingest a directory, resolve product sites, discover contacts

---

## 5. Guardrails

An agent with a scraping tool can spend real money quickly and can behave badly toward third-party sites. Non-negotiable limits:

| Guard | Limit |
|---|---|
| Pages per MCP session | 500, then refuse with a clear message |
| Concurrent crawls per session | 2 |
| Crawl limit via MCP | 500 max, regardless of request |
| `fetchContent` results | 20 max |
| `extract` URLs | 10 per call |
| Proxy bandwidth per session | Configurable cap; refuse past it |
| `executeJavascript` | **Not exposed over MCP at all** |

Refusals are explicit and explain the limit. An agent that hits a cap should understand why and adapt, not retry blindly.

The `executeJavascript` exclusion is deliberate: arbitrary script execution in a browser, driven by a model, reachable from prompt content, is not a risk worth taking. It stays REST-only, behind a per-key flag.

---

## 6. Error responses

Errors are written for a model to act on, not for a developer to grep.

Bad:
```
Error: BLOCKED
```

Good:
```
Could not fetch this page — the site blocked the request at every method tried.
This site has strong bot protection. Options: try a different source for this
information, or check whether the content is available via the site's API.
```

Include what failed, why, and what to try instead. An agent given a bare error code retries the identical request.

---

## 7. Implementation notes

- Use the official MCP Python SDK
- Same core modules as the REST API — import them, do not duplicate
- Tool schemas from the same Pydantic models, narrowed for MCP defaults and caps
- Long operations return job ids immediately; never block an MCP call on a crawl
- Log every MCP tool call with session id, tool, arguments and cost. This is the audit trail for what an agent did

### Auth

- stdio: trusted by process boundary, no auth. Work is attributed to the dedicated `mcp-session` key and is not metered.
- HTTP: the hosted endpoint at `POST /mcp` on the API itself (streamable HTTP, JSON responses). `Authorization: Bearer <api key>` from the same `api_keys` table as REST, the same per-key rate limit, and the same metering — a customer's agent spends the customer's credits exactly as their code would, and every call lands in the same usage ledger with its endpoint. A key with no owner (an operator key) is not metered, as over REST. Refusals use the REST envelope with a `WWW-Authenticate: Bearer` header on a 401.
- Guardrails are per MCP session, keyed by the transport's `Mcp-Session-Id`, forgotten after an idle hour. A crawl started over MCP belongs to the calling key; `crawlStatus` and `crawlPages` answer "not found" for another key's job, as `/v1/crawl/{id}` does.
- `executeJavascript` remains unreachable over MCP whatever the key's flags.

The endpoint is internet-facing by design — this is what Cursor, Claude Code, VS Code and the rest connect to — on the same footing as `/v1`: a bearer key over HTTPS, no OAuth layer. `ENGINE_MCP_HTTP_ENABLED=false` removes the route for a deployment that wants stdio only.

---

## 8. Testing

- Every tool: schema validation, happy path, error path
- Truncation: assert `maxChars` respected and the marker present
- Guardrails: assert each limit fires and returns an actionable message
- Parity: for each tool, assert the result matches the equivalent REST call for the same input. This is the test that stops the two surfaces drifting
- Context budget: assert no tool can return more than its declared maximum, including in the error path
