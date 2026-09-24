"""Domain pacing is an opinion, not a schema default.

`politeness_delay_ms` and `max_concurrency` were NOT NULL with defaults of
1000 and 2 — the free plan's pacing. Every insert into domain_profiles, for
any reason at all (country tracking, tier memory, statistics), therefore
stamped those values onto the domain, and the reader could not tell them from
a deliberate decision.

The effect: 3,594 of 3,652 domains carried "1000ms, 2 at a time" as an
explicit override that beat the caller's plan. Per-host pacing scaled with the
plan for exactly the domains nobody had ever touched, and for nothing else.

Nullable, no default: a row now says "no opinion" unless something set one. The
58 domains that were genuinely slowed (a 429's Retry-After, a crawl-delay) keep
their values.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# What the columns used to default to, and therefore what "never decided"
# looks like in the existing rows.
LEGACY_DELAY_MS = 1000
LEGACY_CONCURRENCY = 2


def upgrade() -> None:
    op.execute("ALTER TABLE domain_profiles ALTER COLUMN politeness_delay_ms DROP DEFAULT")
    op.execute("ALTER TABLE domain_profiles ALTER COLUMN max_concurrency DROP DEFAULT")
    op.execute("ALTER TABLE domain_profiles ALTER COLUMN politeness_delay_ms DROP NOT NULL")
    op.execute("ALTER TABLE domain_profiles ALTER COLUMN max_concurrency DROP NOT NULL")

    # Clear only the values that ARE the old defaults. A domain deliberately
    # slowed above them is a real decision and is left exactly as it is.
    op.execute(
        f"""
        UPDATE domain_profiles
        SET politeness_delay_ms = NULL
        WHERE politeness_delay_ms = {LEGACY_DELAY_MS}
        """  # noqa: S608 - LEGACY_DELAY_MS is the int literal above, not input
    )
    op.execute(
        f"""
        UPDATE domain_profiles
        SET max_concurrency = NULL
        WHERE max_concurrency = {LEGACY_CONCURRENCY}
        """  # noqa: S608 - LEGACY_CONCURRENCY is the int literal above, not input
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE domain_profiles SET politeness_delay_ms = {LEGACY_DELAY_MS} "  # noqa: S608 - int literal, not input
        "WHERE politeness_delay_ms IS NULL"
    )
    op.execute(
        f"UPDATE domain_profiles SET max_concurrency = {LEGACY_CONCURRENCY} "  # noqa: S608 - int literal, not input
        "WHERE max_concurrency IS NULL"
    )
    op.execute("ALTER TABLE domain_profiles ALTER COLUMN politeness_delay_ms SET NOT NULL")
    op.execute("ALTER TABLE domain_profiles ALTER COLUMN max_concurrency SET NOT NULL")
    op.execute(
        "ALTER TABLE domain_profiles ALTER COLUMN politeness_delay_ms "
        f"SET DEFAULT {LEGACY_DELAY_MS}"
    )
    op.execute(
        f"ALTER TABLE domain_profiles ALTER COLUMN max_concurrency SET DEFAULT {LEGACY_CONCURRENCY}"
    )
