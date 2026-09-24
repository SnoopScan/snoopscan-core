"""Find Leads: a job that finds businesses by what they do and where.

The job runs on the ordinary jobs table under a new kind, with its progress in
`stage` (what it is doing now, shown to the caller while it runs) and its
results in `lead_results`, one row per business delivered, in the order they
are shown. Per-lead prices join the credit table the desk edits.
"""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Allowed inside a transaction since PostgreSQL 12; the value is only used
    # by later transactions, never by this one.
    op.execute("ALTER TYPE job_kind ADD VALUE IF NOT EXISTS 'leads'")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS stage text")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS lead_results (
            job_id     text NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            position   integer NOT NULL,
            data       jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (job_id, position)
        )
        """
    )
    op.execute(
        """
        INSERT INTO credit_costs (key, credits, updated_at)
        SELECT v.key, v.credits, now()
        FROM (VALUES ('lead', 2), ('lead_contacts', 1), ('lead_person', 2)) AS v(key, credits)
        WHERE EXISTS (SELECT 1 FROM credit_costs)
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM credit_costs WHERE key IN ('lead', 'lead_contacts', 'lead_person')")
    op.execute("DROP TABLE IF EXISTS lead_results")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS stage")
    # An enum value cannot be dropped; a stray 'leads' is harmless.
