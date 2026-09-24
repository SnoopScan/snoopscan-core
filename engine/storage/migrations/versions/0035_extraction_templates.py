"""Desk-managed extraction templates.

The shipped templates (templates.yaml) are the floor. A field list stops
matching what sites publish the moment a big platform changes its markup, and
waiting for a deploy to add a field — or a whole template for a page type we
did not ship — is the wrong shape of problem. So the desk can add one, or
override a shipped one by using its name.
"""

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE extraction_templates (
            name        text PRIMARY KEY,
            description text NOT NULL DEFAULT '',
            schema      jsonb NOT NULL,
            active      boolean NOT NULL DEFAULT true,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS extraction_templates")
