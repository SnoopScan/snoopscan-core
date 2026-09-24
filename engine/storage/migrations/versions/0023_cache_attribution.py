"""Record whose spend populated a cached page.

The cache is shared across every customer and a hit priced at zero, so the
99th account to ask for a URL paid nothing for work the 1st account paid for.
Measured 8 Sep 2026: 22,162 pages sat inside the live 48-hour window, worth
38,441 credits at our own table, billing nothing however many accounts read
them.

Firecrawl runs the same 48-hour default (`maxAge = 172800000`) and its docs
say plainly: "Cached results still cost 1 credit per page. Caching improves
speed and latency, not credit usage." SerpApi's free cache is a ONE-HOUR
window, which is a retry convenience rather than a corpus to harvest. Free
cross-tenant reads on a two-day window is a position nobody else holds.

`fetched_by` is the OWNER ref, not the key id, so a customer's second key
still reads their own rows free. NULL means a row stored before this ran, or
by something with no owner (a monitor, a warm-up); those are treated as
somebody else's, which is the safe direction — it charges rather than gives
away, and it costs at most one re-fetch to correct as rows age out.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE pages ADD COLUMN IF NOT EXISTS fetched_by text")


def downgrade() -> None:
    op.execute("ALTER TABLE pages DROP COLUMN IF EXISTS fetched_by")
