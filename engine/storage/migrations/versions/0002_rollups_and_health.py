"""Usage rollups and directory health.

Two problems, both of the kind that only appear once there is real volume.

`proxy_usage` is written per request and read on every proxied fetch — the
budget check sums the day's bytes before each one. At a few thousand rows that
is free; at a few million a day it is a growing scan in the hot path of every
fetch, and the failure mode is the whole engine getting slower rather than
anything erroring. So spend is rolled up daily and the raw rows become prunable.

`directories` records `last_item_count` but nothing compares one run to the
last. A directory scraper whose target changed its HTML does not fail — it
succeeds and returns twelve items where it used to return four hundred. That
is invisible in every success metric there is, so the previous count has to be
kept in order to notice.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE proxy_usage_daily (
            day             date        NOT NULL,
            proxy_id        text        NOT NULL,
            domain          text        NOT NULL,
            bytes           bigint      NOT NULL DEFAULT 0,
            requests        integer     NOT NULL DEFAULT 0,
            successes       integer     NOT NULL DEFAULT 0,
            rolled_up_at    timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (day, proxy_id, domain)
        )
        """
    )
    op.execute("CREATE INDEX idx_proxy_usage_daily_day ON proxy_usage_daily (day DESC)")

    # Directory health. `last_item_count` already exists; what was missing is
    # anything to compare it against.
    op.execute(
        """
        ALTER TABLE directories
            ADD COLUMN previous_item_count integer,
            ADD COLUMN last_ingest_ok      boolean NOT NULL DEFAULT true,
            ADD COLUMN health_note         text
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE directories
            DROP COLUMN previous_item_count,
            DROP COLUMN last_ingest_ok,
            DROP COLUMN health_note
        """
    )
    op.execute("DROP TABLE proxy_usage_daily")
