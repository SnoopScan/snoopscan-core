"""Let the circuit breaker back off, and let it see slow failure.

Two faults in one breaker, measured 9 Sep 2026 on the three domains with
zero lifetime successes:

    wisdomlib.org   542 attempts over 33h, 70 min fetch time, 14.7 MB, 0 ok
    hamariweb.com    76 attempts over 30h
    namexray.com     22 attempts over  9h

The breaker never opened for any of them. Its window was "attempts in the last
five minutes" with a twenty-sample minimum, and a domain failing at three or
four requests a minute — politely paced, as a crawl is — never filled it.
wisdomlib peaked at 19. The window is now the last twenty attempts within a
day, which is fixed in the query and needs no schema.

The second fault does. When the breaker DID open it stayed open a flat fifteen
minutes and then let a full-ladder retry through, so a domain that has never
worked at any rung was re-learned sixteen times an hour at top-rung prices.
`circuit_opens` counts consecutive openings without a success; each doubles
the next duration, capped at a day, and any content-success clears it.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE domain_profiles
        ADD COLUMN IF NOT EXISTS circuit_opens integer NOT NULL DEFAULT 0
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE domain_profiles DROP COLUMN IF EXISTS circuit_opens")
