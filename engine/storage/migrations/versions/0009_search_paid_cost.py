"""Price a bought search in the operator's own cost table.

`credit_costs` is edited from the desk, and whatever is saved there is used in
place of the code defaults. So a key added in code after the operator last saved
is missing from their table — and a missing key used to price at zero. A search
answered by a rung we pay for would have been given away.

The code now layers the operator's table over the defaults so absence can never
mean free. This backfills the row so the desk shows the price rather than a gap,
and only where the table is already populated: an install that has never saved
still reads the defaults and needs no row.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO credit_costs (key, credits, updated_at)
        SELECT 'search_paid', 10, now()
        WHERE EXISTS (SELECT 1 FROM credit_costs)
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM credit_costs WHERE key = 'search_paid'")
