"""Remember what each domain's pages look like, so a template can be spotted.

A site that hands back its chrome instead of the page returns the SAME BYTES
for every URL. behindthename.com served a 3,836-character menu bar for
/name/aspen and /name/brandon — byte-identical, sha256 a88a91d43ebb — and
ancestry did the same before it. Both cleared every length threshold, because
3.8 KB of nav is not thin; it is simply the wrong page.

No per-page heuristic can see this: one such response is indistinguishable
from a real short page. It is only visible ACROSS urls, so the check needs a
little memory. This table is that memory and nothing else — domain, content
hash, which URL, when. Rows older than the window are pruned.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS content_fingerprints (
            domain       text   NOT NULL,
            content_hash bytea  NOT NULL,
            url_hash     bytea  NOT NULL,
            seen_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (domain, content_hash, url_hash)
        )
        """
    )
    # The lookup is always "how many distinct URLs on this domain produced
    # this exact body, recently".
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_content_fingerprints_lookup
        ON content_fingerprints (domain, content_hash, seen_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_content_fingerprints_age
        ON content_fingerprints (seen_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS content_fingerprints")
