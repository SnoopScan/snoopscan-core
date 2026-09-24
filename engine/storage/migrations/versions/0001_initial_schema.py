"""Initial schema — the full data model from 02-data-model.md.

Every index here is intentional; do not add more without a measured query.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # -- shared trigger for updated_at ------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    # -- 1. auth ----------------------------------------------------------
    op.execute(
        """
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
        """
    )
    op.execute("CREATE INDEX idx_api_keys_hash ON api_keys (key_hash) WHERE active")

    # -- 2. jobs ----------------------------------------------------------
    op.execute("CREATE TYPE job_kind   AS ENUM ('scrape','crawl','batch','map','extract','search')")
    op.execute(
        "CREATE TYPE job_status AS ENUM ('queued','running','completed','failed','cancelled')"
    )
    op.execute(
        """
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
        """
    )
    op.execute("CREATE INDEX idx_jobs_status ON jobs (status) WHERE status IN ('queued','running')")
    op.execute("CREATE INDEX idx_jobs_key ON jobs (api_key_id, created_at DESC)")
    op.execute("CREATE INDEX idx_jobs_expires ON jobs (expires_at) WHERE status = 'completed'")

    # -- 3. crawl frontier ------------------------------------------------
    op.execute(
        "CREATE TYPE frontier_status AS ENUM ('pending','claimed','done','failed','skipped')"
    )
    op.execute(
        """
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
        """
    )
    # Dedup is a database constraint, not application logic.
    op.execute("CREATE UNIQUE INDEX idx_frontier_dedup ON frontier (job_id, normalized_hash)")
    op.execute(
        "CREATE INDEX idx_frontier_pending ON frontier (job_id, depth, id) WHERE status = 'pending'"
    )
    op.execute("CREATE INDEX idx_frontier_stale ON frontier (claimed_at) WHERE status = 'claimed'")

    # -- 4. pages ---------------------------------------------------------
    op.execute(
        """
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
        """
    )
    op.execute("CREATE INDEX idx_pages_job ON pages (job_id, fetched_at DESC)")
    op.execute("CREATE INDEX idx_pages_lookup ON pages (normalized_hash, fetched_at DESC) WHERE ok")
    op.execute(
        "CREATE INDEX idx_pages_content ON pages (content_hash) WHERE content_hash IS NOT NULL"
    )
    op.execute("CREATE INDEX idx_pages_expires ON pages (expires_at) WHERE expires_at IS NOT NULL")
    # Idempotency: workers upsert on (job_id, normalized_hash) rather than
    # blind-inserting, so a reaped-and-rerun claim cannot duplicate a row.
    op.execute(
        "CREATE UNIQUE INDEX idx_pages_job_dedup ON pages (job_id, normalized_hash) "
        "WHERE job_id IS NOT NULL"
    )

    # -- 5. change tracking ------------------------------------------------
    op.execute(
        """
        CREATE TABLE page_versions (
            id              bigserial PRIMARY KEY,
            normalized_hash bytea NOT NULL,
            url             text NOT NULL,
            content_hash    bytea NOT NULL,
            word_count      integer,
            captured_at     timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_versions_lookup ON page_versions (normalized_hash, captured_at DESC)"
    )

    # -- 6. domain intelligence -------------------------------------------
    op.execute(
        """
        CREATE TABLE domain_profiles (
            domain               text PRIMARY KEY,

            min_working_tier     text NOT NULL DEFAULT 'http',
            requires_proxy       boolean NOT NULL DEFAULT false,
            required_proxy_type  text,

            detected_waf         text,

            success_count        bigint NOT NULL DEFAULT 0,
            failure_count        bigint NOT NULL DEFAULT 0,
            block_count          bigint NOT NULL DEFAULT 0,

            avg_content_length   integer,
            stdev_content_length integer,
            typical_page_type    text,

            politeness_delay_ms  integer NOT NULL DEFAULT 1000,
            max_concurrency      integer NOT NULL DEFAULT 2,

            robots_txt           text,
            robots_fetched_at    timestamptz,

            circuit_open_until   timestamptz,

            last_success_at      timestamptz,
            last_block_at        timestamptz,
            updated_at           timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE TRIGGER trg_domain_profiles_updated BEFORE UPDATE ON domain_profiles "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # -- 7. proxy pool ------------------------------------------------------
    op.execute("CREATE TYPE proxy_type AS ENUM ('datacenter','residential','mobile')")
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE TABLE proxy_domain_scores (
            proxy_id        text NOT NULL REFERENCES proxies(id) ON DELETE CASCADE,
            domain          text NOT NULL,
            success_count   integer NOT NULL DEFAULT 0,
            failure_count   integer NOT NULL DEFAULT 0,
            block_count     integer NOT NULL DEFAULT 0,
            avg_latency_ms  integer,
            last_used_at    timestamptz,
            last_block_at   timestamptz,
            cooldown_until  timestamptz,
            score           real NOT NULL DEFAULT 0.5,
            PRIMARY KEY (proxy_id, domain)
        );
        """
    )
    op.execute("CREATE INDEX idx_proxy_scores_pick ON proxy_domain_scores (domain, score DESC)")
    op.execute(
        """
        CREATE TABLE proxy_usage (
            id          bigserial PRIMARY KEY,
            proxy_id    text NOT NULL REFERENCES proxies(id),
            job_id      text,
            attempt_id  text,
            domain      text NOT NULL,
            bytes       bigint NOT NULL,
            success     boolean NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_proxy_usage_time ON proxy_usage (recorded_at)")
    # Attempt id keys the usage row so a re-run cannot double-count bandwidth.
    op.execute(
        "CREATE UNIQUE INDEX idx_proxy_usage_attempt ON proxy_usage (attempt_id) "
        "WHERE attempt_id IS NOT NULL"
    )

    # -- 8. lead generation -------------------------------------------------
    op.execute(
        """
        CREATE TABLE directories (
            id               text PRIMARY KEY,
            name             text NOT NULL,
            base_url         text NOT NULL,
            ingest_method    text NOT NULL,
            api_config       jsonb,
            active           boolean NOT NULL DEFAULT true,
            last_ingested_at timestamptz,
            last_item_count  integer,
            created_at       timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
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
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_products_external ON products (directory_id, external_id) "
        "WHERE external_id IS NOT NULL"
    )
    op.execute("CREATE INDEX idx_products_domain ON products (company_domain)")
    op.execute(
        """
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
        """
    )
    op.execute(
        """
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
        """
    )
    op.execute("CREATE UNIQUE INDEX idx_contacts_email ON contacts (email_normalized)")
    op.execute("CREATE INDEX idx_contacts_company ON contacts (company_id)")
    op.execute(
        "CREATE INDEX idx_contacts_sendable ON contacts (verification_status) WHERE NOT suppressed"
    )
    # The most important table in the schema: a suppression must survive
    # deletion and re-scraping of the contact row, so it lives separately and
    # is never deleted.
    op.execute(
        """
        CREATE TABLE suppression_list (
            email_normalized text PRIMARY KEY,
            reason           text NOT NULL,
            added_at         timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    # -- 9. observability ---------------------------------------------------
    # One row per TIER ATTEMPT, not per request — this is what makes
    # escalation waste visible.
    op.execute(
        """
        CREATE TABLE fetch_log (
            id           bigserial PRIMARY KEY,
            job_id       text,
            domain       text NOT NULL,
            url_hash     bytea NOT NULL,
            tier         text NOT NULL,
            outcome      text NOT NULL,
            status_code  integer,
            latency_ms   integer,
            bytes        bigint,
            proxy_id     text,
            block_signal text,
            recorded_at  timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_fetch_log_time ON fetch_log (recorded_at)")
    op.execute("CREATE INDEX idx_fetch_log_domain ON fetch_log (domain, recorded_at DESC)")


def downgrade() -> None:
    for table in (
        "fetch_log",
        "suppression_list",
        "contacts",
        "companies",
        "products",
        "directories",
        "proxy_usage",
        "proxy_domain_scores",
        "proxies",
        "domain_profiles",
        "page_versions",
        "pages",
        "frontier",
        "jobs",
        "api_keys",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for enum in ("proxy_type", "frontier_status", "job_status", "job_kind"):
        op.execute(f"DROP TYPE IF EXISTS {enum}")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
