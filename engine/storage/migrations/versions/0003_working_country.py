"""Remember which proxy country a domain will actually answer.

Measured, not assumed. On a 105-URL checkpoint, etsy.com returned 403 to every
request from a GB residential IP and 200 to every request from a US one — ten
out of ten each way. Nothing in the engine could learn that: the proxy country
came only from the caller passing `location`, so a geo-gated domain escalated
to the browser tier, which costs ~40x and does not work either.

One column, the same learn-once shape already used for `min_working_tier` and
`required_proxy_type`.

`country_attempts` records what has been tried so a domain that answers from
nowhere is not retried around the world on every request — the retry has to be
bounded or a geo-gate becomes a per-request cost multiplier.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE domain_profiles
            ADD COLUMN working_country  text,
            ADD COLUMN country_attempts text[] NOT NULL DEFAULT '{}'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE domain_profiles
            DROP COLUMN working_country,
            DROP COLUMN country_attempts
        """
    )
