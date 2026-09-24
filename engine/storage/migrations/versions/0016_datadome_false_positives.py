"""Undo the profiles the DataDome header rule poisoned.

Until 7 Sep 2026 the validator called any response carrying `x-datadome` or a
`datadome` cookie a block. Both ride on every response from a protected site,
allowed ones included, so real pages were recorded as blocks: eight domains
with 46 blocks and not one success between them, thebump.com among them — a
500KB article reachable at tier 1, paying for stealth_hard.

Reset only what that rule could have written: the vendor, the block count and
the raised floor, and only on domains with no success to contradict the reset.
A domain that genuinely challenges re-marks itself on its next request under
the corrected rule.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE domain_profiles
        SET detected_waf = NULL, block_count = 0, min_working_tier = 'http'
        WHERE detected_waf = 'datadome' AND success_count = 0
        """
    )


def downgrade() -> None:
    # The evidence that produced those rows was wrong; there is nothing true
    # to restore.
    pass
