"""Monitors: watch pages for changes on a schedule.

A monitor names a set of URLs, an interval, and where to tell someone. Each run
is a check: every URL scraped with change tracking, the result per page (same,
changed, new, error), the counts, and a webhook if one was asked for. The
comparison itself is the change-tracking machinery /v1/scrape already has; this
is the table that remembers to run it.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS monitors (
            id               text PRIMARY KEY,
            api_key_id       text NOT NULL REFERENCES api_keys(id),
            name             text NOT NULL,
            urls             jsonb NOT NULL,
            interval_minutes integer NOT NULL CHECK (interval_minutes >= 5),
            goal             text,
            webhook_url      text,
            active           boolean NOT NULL DEFAULT true,
            created_at       timestamptz NOT NULL DEFAULT now(),
            last_run_at      timestamptz,
            next_run_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_monitors_due ON monitors (next_run_at) WHERE active")
    op.execute("CREATE INDEX IF NOT EXISTS idx_monitors_key ON monitors (api_key_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS monitor_checks (
            id           text PRIMARY KEY,
            monitor_id   text NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
            started_at   timestamptz NOT NULL DEFAULT now(),
            finished_at  timestamptz,
            pages        jsonb NOT NULL DEFAULT '[]',
            same         integer NOT NULL DEFAULT 0,
            changed      integer NOT NULL DEFAULT 0,
            new          integer NOT NULL DEFAULT 0,
            errors       integer NOT NULL DEFAULT 0,
            triggered_by text NOT NULL DEFAULT 'schedule'
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_monitor_checks_monitor "
        "ON monitor_checks (monitor_id, started_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS monitor_checks")
    op.execute("DROP TABLE IF EXISTS monitors")
