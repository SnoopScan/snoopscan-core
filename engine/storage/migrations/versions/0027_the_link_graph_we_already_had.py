"""Keep the link graph we have been collecting and discarding.

Every scrape extracts a page's outbound links and stores them on the page row.
Nothing has ever read them across pages, so the one question they answer — who
links to this domain — could not be asked. On 9 Sep 2026 there were 3,137,444
links sitting in `pages.links` and no way to query them by target.

Aggregated to domain -> domain they collapse to 51,552 rows over 30,264
distinct target hosts. That is the shape an off-page question actually wants:
"referring domains" is the number the field quotes, not raw link count, and a
sample URL pair on each row is enough to show the link itself.

No backfill here. The domain of a URL is decided by `urls.registrable_domain`,
which consults the public suffix list — `bbc.co.uk` is one domain and
`foo.github.io` is another — and a SQL regex approximating that would be a
second definition that drifts from the first. `tools/backfill_link_graph.py`
fills it using the real function; new pages write on store.

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS domain_links (
            source_domain     text NOT NULL,
            target_domain     text NOT NULL,
            links             integer NOT NULL DEFAULT 1,
            sample_source_url text,
            sample_target_url text,
            first_seen        timestamptz NOT NULL DEFAULT now(),
            last_seen         timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (source_domain, target_domain)
        )
        """
    )
    # The whole point: "who links to X" has to be one index scan. The primary
    # key orders source-first and cannot answer it.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_domain_links_target "
        "ON domain_links (target_domain, links DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS domain_links")
