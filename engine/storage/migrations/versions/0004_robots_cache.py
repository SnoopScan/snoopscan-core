"""Give robots.txt its own table, keyed by host.

`store_robots()` was called with a HOST and wrote into `domain_profiles.domain`,
a column every other writer fills with a REGISTRABLE DOMAIN. So fetching
https://www.etsy.com/ created a phantom `www.etsy.com` profile alongside the
real `etsy.com` one, carrying none of its learned intelligence.

Nothing broke visibly — profile reads use the registrable domain and find the
right row — which is why it survived. What it corrupts is every COUNT and
aggregate over the table. "We have profiled N domains" is the number this
asset is worth, and it was inflated by one phantom per host.

Two features sharing one store with different key semantics. They are separated
here rather than reconciled, because robots.txt is genuinely per-host: RFC 9309
scopes it to scheme+host+port, so `blog.example.com/robots.txt` and
`www.example.com/robots.txt` are different documents and MUST NOT be collapsed
onto one registrable domain. Keying it by domain would have been the other,
worse bug.

Existing robots data is carried across rather than dropped — it is a cache, but
re-fetching it means a burst of requests at every target the moment this ships.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE robots_cache (
            host         text PRIMARY KEY,
            body         text NOT NULL DEFAULT '',
            fetched_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_robots_cache_fetched ON robots_cache (fetched_at)")

    # Carry the cache across, so shipping this does not trigger a robots.txt
    # fetch against every domain we know at once.
    op.execute(
        """
        INSERT INTO robots_cache (host, body, fetched_at)
        SELECT domain, COALESCE(robots_txt, ''), COALESCE(robots_fetched_at, now())
        FROM domain_profiles
        WHERE robots_txt IS NOT NULL
        ON CONFLICT (host) DO NOTHING
        """
    )

    # Now remove the phantom rows: a profile that only ever existed because
    # robots.txt was written to it. Identified by having no learned signal of
    # any kind — never fetched, never blocked, never profiled.
    op.execute(
        """
        DELETE FROM domain_profiles
        WHERE robots_txt IS NOT NULL
          AND success_count = 0 AND failure_count = 0 AND block_count = 0
          AND min_working_tier = 'http'
          AND detected_waf IS NULL
          AND requires_proxy = false
          AND avg_content_length IS NULL
        """
    )

    op.execute("ALTER TABLE domain_profiles DROP COLUMN robots_txt, DROP COLUMN robots_fetched_at")


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE domain_profiles
            ADD COLUMN robots_txt text,
            ADD COLUMN robots_fetched_at timestamptz
        """
    )
    op.execute(
        """
        UPDATE domain_profiles p SET robots_txt = r.body, robots_fetched_at = r.fetched_at
        FROM robots_cache r WHERE r.host = p.domain
        """
    )
    op.execute("DROP TABLE robots_cache")
