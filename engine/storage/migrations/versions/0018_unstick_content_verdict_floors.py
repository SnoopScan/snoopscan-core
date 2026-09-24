"""Release domains stranded by a verdict about their content.

`near_empty` and `link_only` were SOFT_BLOCK until 7 Sep 2026. A soft block
calls apply_block, which raises the domain's tier floor — so every JavaScript
app and every login-gated page we touched was taxed to the 5-credit rung for
ever after, on a judgement about the PAGE rather than the target's behaviour.

Reset only the domains with no WAF vendor recorded and no success to
contradict it: if something had genuinely refused us, a signature or a
challenge title would have named it. A domain that really is hard re-marks
itself on the next request, under rules that now tell a shell from a block.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE domain_profiles
        SET min_working_tier = 'http', block_count = 0
        WHERE min_working_tier <> 'http'
          AND success_count = 0
          AND detected_waf IS NULL
        """
    )


def downgrade() -> None:
    pass  # the evidence that raised these floors was a content judgement
