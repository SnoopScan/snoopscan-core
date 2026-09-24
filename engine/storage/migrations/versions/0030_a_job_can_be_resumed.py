"""Count how many times a job has been picked back up after being orphaned.

A crawl or batch is driven by ONE Redis message, popped destructively. When a
worker stopped or crashed mid-job nothing re-drove it: the job sat `running`
or `queued` for ever. Found 11 Sep 2026 — three such jobs, the oldest from
1 Sep — and spec 07 §1, §2 and §11 all say the job must be retried.

The scheduler now re-queues an orphaned job. `resumes` bounds that: a job that
keeps being orphaned is failed with a clear error rather than retried for
ever, the same bargain the frontier makes with `attempts`.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS resumes integer NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS resumes")
