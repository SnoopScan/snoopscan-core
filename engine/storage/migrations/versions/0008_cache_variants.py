"""The page cache is shared between customers, so its key must describe the page.

Measured 4 September 2026: two scrapes of ipinfo.io, the first through a US exit
and the second explicitly asking for GB, and the GB caller was served the US page
from cache. The key was the URL alone, so every request for a URL collided with
every other one however differently it had been fetched.

Two columns fix it:

  variant_hash      the URL plus everything that changes what the document IS —
                    exit country, mobile rendering. Lookups match on this.
  shared_cacheable  false for a fetch that carried caller-supplied headers or
                    cookies. Those responses are personalised by definition, and
                    the cache serves every customer: one caller's authenticated
                    page must never become another caller's result.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE pages ADD COLUMN variant_hash bytea")
    op.execute("ALTER TABLE pages ADD COLUMN shared_cacheable boolean NOT NULL DEFAULT true")
    # Existing rows have no variant recorded. Leaving variant_hash NULL means
    # they simply never match a lookup, which retires the old cache rather than
    # letting rows keyed the old way answer requests keyed the new way.
    op.execute(
        "CREATE INDEX pages_variant_lookup ON pages (variant_hash, fetched_at DESC) "
        "WHERE shared_cacheable"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS pages_variant_lookup")
    op.execute("ALTER TABLE pages DROP COLUMN IF EXISTS shared_cacheable")
    op.execute("ALTER TABLE pages DROP COLUMN IF EXISTS variant_hash")
