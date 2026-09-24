"""A grade on each proxy provider: budget or premium.

Providers differ by an order of magnitude in price per GB and, measured, in
whether they get through a defended site at all. A single priority order sent
every request to one provider and left the rest idle. With a grade, easy work
on a domain already proven easy goes to the budget providers and everything
else to the premium ones — traffic shared at random within each grade.
"""

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE proxy_providers
            ADD COLUMN grade text NOT NULL DEFAULT 'premium'
            CHECK (grade IN ('budget', 'premium'))
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE proxy_providers DROP COLUMN grade")
