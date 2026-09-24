"""Let a domain's tier floor rise on evidence, not only fall.

`apply_success` only ever LOWERED `min_working_tier`. A success one rung above
the floor recorded nothing, and the only thing that raised a floor was a hard
block. So a domain whose cheap rungs answer 200 with a nav shell — a page that
is thin rather than blocked — kept its floor at the useless rung for ever and
bought a doomed attempt on every single request.

Measured 7 Sep 2026: ancestry.com sat at `stealth` with 199 successes, every
one of them served by `stealth_hard`, one rung higher. Roughly 15-20s of every
ancestry fetch was the failed `stealth` attempt underneath it.

`climbs_above_floor` counts consecutive content-successes above the floor;
three in a row moves it up one rung (escalation.RAISE_FLOOR_AFTER).

No floor is corrected here. It cannot be done from `fetch_log`: that table
records the TRANSPORT outcome, so ancestry's shells are logged as successes at
`http`, `browser` and `stealth`, and a backfill would read them as proof the
cheap rungs work and set the floor lower than it already is — the exact
opposite of the bug. The counter re-learns each floor from content verdicts
within three requests, which is the only signal that can tell the difference.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE domain_profiles
        ADD COLUMN IF NOT EXISTS climbs_above_floor integer NOT NULL DEFAULT 0
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE domain_profiles DROP COLUMN IF EXISTS climbs_above_floor")
