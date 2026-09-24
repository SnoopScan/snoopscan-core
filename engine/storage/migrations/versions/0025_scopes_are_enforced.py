"""Grant existing keys every scope, now that scopes are enforced.

Scopes were stored on every key, offered as four checkboxes in the customer's
dashboard, and checked nowhere. Enforcement starts with this release.

Every key in the wild was created under the old behaviour, where the four
scopes it holds meant nothing and it could call all eleven surfaces. Turning
enforcement on without this migration would silently revoke access a customer
already had and is using — a working integration would start returning 403 on
products, posts, places, extract, batch, monitor and parse, with no action on
their part. So existing keys are widened to everything they could already do.

New keys get exactly what the customer ticks. The narrowing is theirs to make,
not ours to impose retroactively.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

# engine/api/scopes.py ALL_SCOPES at the time of writing. Spelled out rather
# than imported: a migration must keep doing what it did when it ran, and an
# imported list would change under it.
ALL = [
    "batch",
    "crawl",
    "extract",
    "map",
    "monitor",
    "parse",
    "places",
    "posts",
    "products",
    "scrape",
    "search",
]


def upgrade() -> None:
    op.execute(
        """
        UPDATE api_keys
        SET scopes = ARRAY['batch','crawl','extract','map','monitor',
                           'parse','places','posts','products','scrape','search']
        WHERE active
        """
    )


def downgrade() -> None:
    # No sensible reverse: the previous values were never enforced, so there
    # is nothing to restore that meant anything.
    pass
