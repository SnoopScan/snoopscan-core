"""Raise a tier floor on evidence, not on one block.

`apply_success` has raised the floor only after RAISE_FLOOR_AFTER consecutive
content-successes above it since 0019, with a comment saying why: "never
straight to `tier`, so a one-off hard climb cannot pin a whole domain to the
expensive end." `apply_block` moved the SAME floor on a single block.

Measured 9 Sep 2026 across 4,112 profiles: 226 of the 347 raised floors had
been raised by exactly ONE block.

    reddit.com   floor=mobile       47 successes, 1 block
                 stealth_hard served it 190 times, mobile 13
    asana.com    floor=impersonate  http succeeded 4 times and never blocked

The cost is not symmetric, which is what makes one block the wrong bar:

    floor too LOW   one cheap wasted attempt, and the request still climbs
                    and succeeds inside itself
    floor too HIGH  every later request to that domain pays the dear rung
                    until the 30-day decay

`blocks_at_floor` is the mirror of `climbs_above_floor`: consecutive blocks AT
the floor, cleared by any content-success.

The floors raised on one block are lowered by ONE rung here, which is the
state they were in before that block. This is safe in a way the 0019 backfill
was not: that one would have had to read `fetch_log`, which records TRANSPORT
outcomes and cannot tell a nav shell from a page. This reads only the profile's
own block count, and being wrong is self-correcting in either direction —
`apply_success` puts a floor back up after three climbs, `apply_block` after
three blocks, and both cost cheap attempts rather than dear ones.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

# The ladder, as SQL. Kept here rather than imported so the migration means
# the same thing in five years' time when the tiers have moved on.
ONE_RUNG_DOWN = """
    CASE min_working_tier
        WHEN 'mobile'       THEN 'stealth_hard'
        WHEN 'stealth_hard' THEN 'stealth'
        WHEN 'stealth'      THEN 'browser'
        WHEN 'browser'      THEN 'impersonate'
        WHEN 'impersonate'  THEN 'http'
        ELSE min_working_tier
    END
"""


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE domain_profiles
        ADD COLUMN IF NOT EXISTS blocks_at_floor integer NOT NULL DEFAULT 0
        """
    )
    # Only where a single block can have been the cause, and only where
    # nothing is currently arguing the floor should be HIGHER.
    op.execute(
        f"""
        UPDATE domain_profiles
        SET min_working_tier = {ONE_RUNG_DOWN}
        WHERE block_count = 1
          AND min_working_tier <> 'http'
          AND climbs_above_floor = 0
        """  # noqa: S608 - ONE_RUNG_DOWN is the static CASE-expression string above, not input
    )


def downgrade() -> None:
    # The lowered floors are not restored: they were raised on evidence this
    # schema no longer accepts, and the running system re-learns either way.
    op.execute("ALTER TABLE domain_profiles DROP COLUMN IF EXISTS blocks_at_floor")
