"""Stop serving cached pages that today's validator would refuse.

The THIN verdict (empty body, navigation shell) was added on 7 Sep 2026. Rows
stored before it were never judged by it and were served as cache hits — a
pilot's three remaining silent failures were all exactly this: a
608-byte menu at tier http, bytes=0, ms=0.

The read path now re-judges every hit, so this is belt as well as braces: mark
the rows uncacheable so they are never even candidates. Not deleted — they are
also job results, and a job's history is a job's history.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE pages SET shared_cacheable = false
        WHERE ok AND shared_cacheable
          AND (COALESCE(word_count, 0) < 40
               OR (extraction_path = 'fallback' AND COALESCE(word_count, 0) < 200))
        """
    )


def downgrade() -> None:
    pass  # nothing true to restore
