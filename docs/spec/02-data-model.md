# 02 — Data Model

PostgreSQL 15+. Source of truth for everything durable. Redis holds only the transient queue.

Migrations via Alembic. Every migration reversible. No destructive migration without an explicit backup step in the runbook.

## Conventions

- Primary keys: prefixed ULIDs as `text` (`crawl_01J8Z...`, `page_01J8Z...`). Sortable by creation time, readable in logs, no coordination needed
- Timestamps: `timestamptz`, always UTC, default `now()`
- JSON: `jsonb`, never `json`
- Soft delete only where retention policy requires it; everything else hard-deletes
- Every table has `created_at`; mutable tables have `updated_at` maintained by trigger

---

## 1. Auth and tenancy

```sql
CREATE TABLE api_keys (
    id              text PRIMARY KEY,
    key_hash        text NOT NULL UNIQUE,
    label           text NOT NULL,
    scopes          text[] NOT NULL DEFAULT '{scrape,crawl,map}',
    rate_limit_rpm  integer NOT NULL DEFAULT 60,
    allow_js_exec   boolean NOT NULL DEFAULT false,
    webhook_secret  text,
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_used_at    timestamptz
);

CREATE INDEX idx_api_keys_hash ON api_keys (key_hash) WHERE active;
```

Store `key_hash` as SHA-256 of the key. The plaintext key is shown once at creation and never again.

`allow_js_exec` gates the `executeJavascript` action. Off by default.

A `tenant_id` column is deliberately omitted. Adding multi-tenancy later is a migration; building it now is speculative work. The key **is** the tenant boundary for the moment.

---

## 2. Jobs

```sql
CREATE TYPE job_kind   AS ENUM ('scrape','crawl','batch','map','extract','search');
CREATE TYPE job_status AS ENUM ('queued','running','completed','failed','cancelled');

CREATE TABLE jobs (
    id              text PRIMARY KEY,
    kind            job_kind NOT NULL,
    status          job_status NOT NULL DEFAULT 'queued',
    api_key_id      text NOT NULL REFERENCES api_keys(id),

    input           jsonb NOT NULL,

    total           integer NOT NULL DEFAULT 0,
    completed       integer NOT NULL DEFAULT 0,
    failed          integer NOT NULL DEFAULT 0,

    cost            jsonb NOT NULL DEFAULT '{}',

    error           jsonb,

    webhook_url     text,
    webhook_events  text[],

    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    completed_at    timestamptz,
    expires_at      timestamptz NOT NULL
);

CREATE INDEX idx_jobs_status  ON jobs (status) WHERE status IN ('queued','running');
CREATE INDEX idx_jobs_key     ON jobs (api_key_id, created_at DESC);
CREATE INDEX idx_jobs_expires ON jobs (expires_at) WHERE status = 'completed';
```

`input` stores the full validated request. Reproducibility: any job can be replayed from it.

`cost` accumulates across the job — `proxy_bytes`, `browser_ms`, `tier_breakdown`.

`expires_at` set at creation from the retention policy. A sweeper deletes expired rows.

---

## 3. Crawl frontier

```sql
CREATE TYPE frontier_status AS ENUM ('pending','claimed','done','failed','skipped');

CREATE TABLE frontier (
    id              bigserial PRIMARY KEY,
    job_id          text NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,

    url             text NOT NULL,
    url_hash        bytea NOT NULL,
    normalized_hash bytea NOT NULL,

    depth           integer NOT NULL DEFAULT 0,
    parent_url      text,
    discovered_via  text,

    status          frontier_status NOT NULL DEFAULT 'pending',
    attempts        integer NOT NULL DEFAULT 0,
    claimed_at      timestamptz,
    claimed_by      text,

    skip_reason     text,

    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_frontier_dedup ON frontier (job_id, normalized_hash);
CREATE INDEX idx_frontier_pending ON frontier (job_id, depth, id) WHERE status = 'pending';
CREATE INDEX idx_frontier_stale   ON frontier (claimed_at) WHERE status = 'claimed';
```

Two hashes on purpose:

- `url_hash` — SHA-256 of the exact URL. Identity.
- `normalized_hash` — SHA-256 after normalisation (lowercase host, strip default port, strip trailing slash, sort query params, drop tracking params, optionally drop all query params). Deduplication.

The unique index on `(job_id, normalized_hash)` makes dedup a database constraint rather than application logic. Insert with `ON CONFLICT DO NOTHING`.

**Claiming** is a single atomic statement — no advisory locks, no select-then-update race:

```sql
UPDATE frontier SET status='claimed', claimed_at=now(), claimed_by=$worker
WHERE id = (
  SELECT id FROM frontier
  WHERE job_id=$job AND status='pending'
  ORDER BY depth, id
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *;
```

A reaper returns rows `claimed` for longer than the job timeout to `pending` and increments `attempts`. At `attempts >= 3` the row goes to `failed`.

Breadth-first via `ORDER BY depth, id`. Shallow pages are usually the valuable ones, and a depth-first crawl that hits a calendar widget never returns.

---

## 4. Pages

```sql
CREATE TABLE pages (
    id                    text PRIMARY KEY,
    job_id                text REFERENCES jobs(id) ON DELETE CASCADE,

    url                   text NOT NULL,
    source_url            text NOT NULL,
    normalized_hash       bytea NOT NULL,

    status_code           integer,
    content_type          text,

    content_hash          bytea,
    markdown              text,
    html                  text,
    raw_html              text,
    links                 jsonb,
    structured            jsonb,
    screenshot_path       text,

    title                 text,
    description           text,
    language              text,
    author                text,
    published_at          timestamptz,

    page_type             text,
    word_count            integer,
    extraction_confidence real,
    extraction_path       text,

    fetch_tier            text,
    tiers_attempted       text[],
    proxy_type            text,
    proxy_bytes           bigint DEFAULT 0,
    browser_ms            integer DEFAULT 0,

    ok                    boolean NOT NULL DEFAULT true,
    error_code            text,
    block_signals         jsonb,

    fetched_at            timestamptz NOT NULL DEFAULT now(),
    expires_at            timestamptz
);

CREATE INDEX idx_pages_job      ON pages (job_id, fetched_at DESC);
CREATE INDEX idx_pages_lookup   ON pages (normalized_hash, fetched_at DESC) WHERE ok;
CREATE INDEX idx_pages_content  ON pages (content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX idx_pages_expires  ON pages (expires_at) WHERE expires_at IS NOT NULL;
```

`raw_html` is large and rarely needed. Store it only when explicitly requested, and consider moving it to object storage with a pointer once volume grows. **Watch this column** — it will dominate table size.

`content_hash` is SHA-256 of normalised extracted text (whitespace collapsed, lowercased). Drives change detection and cross-URL duplicate detection.

`block_signals` records what the detector saw even on success — useful for tuning thresholds later.

### Cache lookup

The cache is this table, not a separate one. `maxAge` resolves to:

```sql
SELECT * FROM pages
WHERE normalized_hash = $1 AND ok
  AND fetched_at > now() - ($2 || ' milliseconds')::interval
ORDER BY fetched_at DESC LIMIT 1;
```

One store, no invalidation problem, no divergence between cache and record.

---

## 5. Change tracking

```sql
CREATE TABLE page_versions (
    id              bigserial PRIMARY KEY,
    normalized_hash bytea NOT NULL,
    url             text NOT NULL,
    content_hash    bytea NOT NULL,
    word_count      integer,
    captured_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_versions_lookup ON page_versions (normalized_hash, captured_at DESC);
```

Append-only, one row per observed change. If `content_hash` matches the latest row, no row is written.

`changeStatus` derives: no prior row → `new`; hash differs from latest → `changed`; hash matches → `same`. The diff itself is computed on demand from the two `pages` rows, not stored.

---

## 6. Domain intelligence

The per-domain memory behind tier selection. Directly implements principle P1.

```sql
CREATE TABLE domain_profiles (
    domain              text PRIMARY KEY,

    min_working_tier    text NOT NULL DEFAULT 'http',
    requires_proxy      boolean NOT NULL DEFAULT false,
    required_proxy_type text,

    detected_waf        text,

    success_count       bigint NOT NULL DEFAULT 0,
    failure_count       bigint NOT NULL DEFAULT 0,
    block_count         bigint NOT NULL DEFAULT 0,

    avg_content_length  integer,
    stdev_content_length integer,
    typical_page_type   text,

    politeness_delay_ms integer NOT NULL DEFAULT 1000,
    max_concurrency     integer NOT NULL DEFAULT 2,

    robots_txt          text,
    robots_fetched_at   timestamptz,

    circuit_open_until  timestamptz,

    last_success_at     timestamptz,
    last_block_at       timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
```

`min_working_tier` is the starting point for `tier: "auto"`. Raised on repeated blocks, and **decayed downward periodically** — a domain that added Cloudflare may later remove it, and without decay we would pay for the browser tier forever.

`avg_content_length` and `stdev_content_length` power the statistical soft-block check in `05-block-detection.md`. Maintained as a running aggregate over successful fetches.

`circuit_open_until` implements the breaker: when a domain's recent failure rate exceeds threshold, stop hitting it until this timestamp.

`robots_txt` cached with a TTL (24h) so we do not re-fetch it per URL.

---

## 7. Proxy pool

```sql
CREATE TYPE proxy_type AS ENUM ('datacenter','residential','mobile');

CREATE TABLE proxies (
    id              text PRIMARY KEY,
    type            proxy_type NOT NULL,
    endpoint        text NOT NULL,
    username        text,
    password_enc    bytea,
    country         text,
    sticky_capable  boolean NOT NULL DEFAULT false,
    active          boolean NOT NULL DEFAULT true,
    retired_at      timestamptz,
    retired_reason  text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE proxy_domain_scores (
    proxy_id        text NOT NULL REFERENCES proxies(id) ON DELETE CASCADE,
    domain          text NOT NULL,
    success_count   integer NOT NULL DEFAULT 0,
    failure_count   integer NOT NULL DEFAULT 0,
    block_count     integer NOT NULL DEFAULT 0,
    avg_latency_ms  integer,
    last_used_at    timestamptz,
    last_block_at   timestamptz,
    score           real NOT NULL DEFAULT 0.5,
    PRIMARY KEY (proxy_id, domain)
);

CREATE INDEX idx_proxy_scores_pick ON proxy_domain_scores (domain, score DESC);

CREATE TABLE proxy_usage (
    id          bigserial PRIMARY KEY,
    proxy_id    text NOT NULL REFERENCES proxies(id),
    job_id      text,
    domain      text NOT NULL,
    bytes       bigint NOT NULL,
    success     boolean NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_proxy_usage_time ON proxy_usage (recorded_at);
```

`password_enc` encrypted at rest with a key from the environment. Credentials never appear in logs or in `input` payloads.

`proxy_domain_scores` is scored per proxy **per domain** — an IP burnt on one target may be fine elsewhere. Global scoring retires IPs too aggressively.

`proxy_usage` is the bandwidth ledger and the basis of real cost reporting. Roll up daily and prune raw rows after 30 days; this table grows fast.

---

## 8. Lead generation

Detailed in `09-leadgen-pipeline.md`. Schema here for completeness.

```sql
CREATE TABLE directories (
    id              text PRIMARY KEY,
    name            text NOT NULL,
    base_url        text NOT NULL,
    ingest_method   text NOT NULL,
    api_config      jsonb,
    active          boolean NOT NULL DEFAULT true,
    last_ingested_at timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE products (
    id              text PRIMARY KEY,
    directory_id    text NOT NULL REFERENCES directories(id),
    external_id     text,
    name            text NOT NULL,
    tagline         text,
    product_url     text,
    company_domain  text,
    listed_at       timestamptz,
    raw             jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_products_external ON products (directory_id, external_id)
    WHERE external_id IS NOT NULL;
CREATE INDEX idx_products_domain ON products (company_domain);

CREATE TABLE companies (
    id                  text PRIMARY KEY,
    domain              text NOT NULL UNIQUE,
    name                text,
    contact_page_url    text,
    contact_form_url    text,
    contact_form_vendor text,
    social_links        jsonb NOT NULL DEFAULT '{}',
    jurisdiction        text,
    subscriber_type     text,
    discovery_status    text NOT NULL DEFAULT 'pending',
    discovered_at       timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE contacts (
    id                  text PRIMARY KEY,
    company_id          text NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    email               text NOT NULL,
    email_normalized    text NOT NULL,
    source              text NOT NULL,
    source_url          text,
    is_role_account     boolean NOT NULL DEFAULT false,
    is_freemail         boolean NOT NULL DEFAULT false,
    verification_status text NOT NULL DEFAULT 'unverified',
    verification_score  real,
    verified_at         timestamptz,
    suppressed          boolean NOT NULL DEFAULT false,
    suppressed_reason   text,
    notice_sent_at      timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_contacts_email ON contacts (email_normalized);
CREATE INDEX idx_contacts_company ON contacts (company_id);
CREATE INDEX idx_contacts_sendable ON contacts (verification_status)
    WHERE NOT suppressed;

CREATE TABLE suppression_list (
    email_normalized text PRIMARY KEY,
    reason           text NOT NULL,
    added_at         timestamptz NOT NULL DEFAULT now()
);
```

`companies.domain` unique — the dedup key. Two products from the same company collapse to one company row.

`subscriber_type` (`corporate` | `individual` | `unknown`) drives legal segmentation. See `11-compliance.md`.

`notice_sent_at` records the Article 14 transparency notification. Not optional.

`suppression_list` is separate from `contacts.suppressed` deliberately: a suppression must survive deletion and re-scraping of the contact row. **Always check both.**

---

## 9. Observability

```sql
CREATE TABLE fetch_log (
    id              bigserial PRIMARY KEY,
    job_id          text,
    domain          text NOT NULL,
    url_hash        bytea NOT NULL,
    tier            text NOT NULL,
    outcome         text NOT NULL,
    status_code     integer,
    latency_ms      integer,
    bytes           bigint,
    proxy_id        text,
    block_signal    text,
    recorded_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_fetch_log_time   ON fetch_log (recorded_at);
CREATE INDEX idx_fetch_log_domain ON fetch_log (domain, recorded_at DESC);
```

One row per **tier attempt**, not per request. A request escalating through three tiers writes three rows. This is what makes escalation waste visible.

`outcome`: `success` | `blocked` | `timeout` | `error` | `target_error`.

High volume. Partition by month, retain 90 days, aggregate before pruning.

---

## 10. Retention

| Table | Retention | Mechanism |
|---|---|---|
| `jobs` | 30 days after completion | `expires_at` sweeper |
| `frontier` | Cascades with job | FK cascade |
| `pages` | 90 days, or `expires_at` | Sweeper |
| `pages.raw_html` | 7 days | Nulled by sweeper, row kept |
| `page_versions` | Indefinite | Small |
| `fetch_log` | 90 days | Partition drop |
| `proxy_usage` | 30 days raw, aggregates kept | Roll up then delete |
| `contacts` | Per `11-compliance.md` | Manual policy |
| `suppression_list` | **Never deleted** | — |

The sweeper is a scheduled job, batched, `LIMIT`ed, off-peak. Never a single unbounded `DELETE` on a large table.

---

## 11. Migration notes

- Every index above is intentional. Do not add more without a measured query
- `pages` is the growth table. Monitor its size weekly. Partitioning by `fetched_at` month is the first move when it becomes a problem — design the queries so that change is non-breaking
- Statement timeout on the application role: `SET statement_timeout = '30s'`. A runaway query must not hold a connection open indefinitely
- Connection pooling via PgBouncer in transaction mode. Workers open and close connections frequently
