"""Usage events keep the URL, not just its host: an activity log a person
reads names the page, and a row's id can be copied into a support ticket.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE usage_events ADD COLUMN url text")


def downgrade() -> None:
    op.execute("ALTER TABLE usage_events DROP COLUMN url")
