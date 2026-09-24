"""Change history belongs to the account that asked for it.

`page_versions` was keyed by URL alone, so one customer's check answered from
another's history: a first check could say "unchanged since <time>", and that
time was when someone else had fetched the URL. Each account now keeps its
own. Rows written before this carry no owner and match nobody, so every
account's next check of a URL starts fresh as "new".
"""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE page_versions ADD COLUMN IF NOT EXISTS owner_ref text")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_versions_owner_lookup "
        "ON page_versions (owner_ref, normalized_hash, captured_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_versions_owner_lookup")
    op.execute("ALTER TABLE page_versions DROP COLUMN IF EXISTS owner_ref")
