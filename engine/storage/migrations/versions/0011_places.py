"""Places: one row per business the Maps source has seen, and its prices.

`feature_id` is Google's own identity for a place and is unique, so a repeat
search updates the row rather than adding another — and it is the join key the
detail and review endpoints will take later. `first_seen_at`/`last_seen_at`
say how fresh a listing is without a separate crawl log.

The two price rows follow 0009: layered over the defaults in code, backfilled
here only where the operator has already saved a table, so absence can never
mean free.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS places (
            id             text PRIMARY KEY,
            feature_id     text NOT NULL UNIQUE,
            name           text NOT NULL,
            category       text,
            address        text,
            latitude       double precision,
            longitude      double precision,
            rating         real,
            review_count   integer,
            place_url      text NOT NULL,
            website        text,
            phone          text,
            first_seen_at  timestamptz NOT NULL DEFAULT now(),
            last_seen_at   timestamptz NOT NULL DEFAULT now(),
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_places_name ON places (lower(name))")
    op.execute(
        """
        INSERT INTO credit_costs (key, credits, updated_at)
        SELECT v.key, v.credits, now()
        FROM (VALUES ('places_search', 5), ('places_detail', 2)) AS v(key, credits)
        WHERE EXISTS (SELECT 1 FROM credit_costs)
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM credit_costs WHERE key IN ('places_search', 'places_detail')")
    op.execute("DROP TABLE IF EXISTS places")
