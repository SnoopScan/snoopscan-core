"""Learn how long each domain actually needs.

The engine already learns which tier a domain answers on, whether it needs a
proxy, which country it will serve and which WAF sits in front of it. It did
not learn the one thing that decides whether any of that gets a chance: TIME.

etsy.com is the case. Its floor is already correct — `detected_waf: datadome`
starts it at `stealth_hard`, so the ladder is just [stealth_hard, mobile] — but
a single deep attempt costs 15-50s and a challenge there is a coin toss. Inside
the 90s default only two or three attempts fit, so a domain that would answer
on the fourth try returns BLOCKED instead (measured 7 Sep 2026).

Raising the global default would spend that time on every domain, including the
1,600 that answer in under a second. Learned per domain, only the domains that
have proved they need it pay for it.

Welford, the same shape already used for content length, so the mean and
deviation are maintained without keeping every observation.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE domain_profiles
        ADD COLUMN IF NOT EXISTS avg_success_ms   integer,
        ADD COLUMN IF NOT EXISTS stdev_success_ms integer,
        ADD COLUMN IF NOT EXISTS timed_success_count integer NOT NULL DEFAULT 0
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE domain_profiles
        DROP COLUMN IF EXISTS avg_success_ms,
        DROP COLUMN IF EXISTS stdev_success_ms,
        DROP COLUMN IF EXISTS timed_success_count
        """
    )
