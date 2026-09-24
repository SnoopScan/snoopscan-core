"""Report the third bucket: skipped.

A crawl's frontier ends every URL in one of done / failed / skipped, and the
job counters were derived from the first two only. On a live audit crawl
(measured) the API said `total 54, completed 44, failed 2` — eight URLs
in no bucket, unreconcilable by a caller paying per page. They were six
external hosts and two excluded paths, correctly skipped and never reported.

One column, derived from the frontier like the others, so it can never drift.
Backfilled for every existing job.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS skipped integer NOT NULL DEFAULT 0")
    op.execute(
        """
        UPDATE jobs j SET skipped = f.n
        FROM (
            SELECT job_id, count(*) AS n FROM frontier
            WHERE status = 'skipped' GROUP BY job_id
        ) f
        WHERE j.id = f.job_id
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS skipped")
