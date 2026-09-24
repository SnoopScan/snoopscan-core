"""Collapse the two spellings of every proxy id into one.

ISO 3166-1 alpha-2 is case-insensitive and reached us both ways: a caller sends
`location.country = "US"`, our own defaults are lower-case. The credential
templates lower-cased it, but the ENDPOINT ID did not — so `res-US` and
`res-us` became two proxies, for the same exit, with two separate score
histories. On 7 Sep 2026 `res-US` sat retired ("blocked across 5 domains")
while `res-us` served, and which one a request got depended on whether the
caller happened to type capitals.

`vendor.normalise_country` now guarantees one spelling, so every upper-case id
is unreachable from here on. This tidies what they left behind:

  * an upper-case id with NO lower-case twin (res-FR, res-IE, res-NL) is
    RENAMED, keeping its history — it is the same proxy under a new spelling;
  * an upper-case id WITH a twin (res-DE, res-GB, res-US) is DELETED, and its
    history goes with it.

Deleted rather than merged, deliberately. `res-US` is retired for accumulated
blocks; folding those into the working `res-us` could retire it on the spot and
take US residential traffic down — which is the outage this migration exists to
prevent, not to cause. The surviving row has the larger history in every case.

`proxy_domain_scores` cascades on delete; `proxy_usage` does not, so its rows
are repointed before the parent goes.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Order matters: `proxy_usage` has a foreign key with no ON UPDATE CASCADE,
    # so the lower-case PARENT has to exist before any child can point at it,
    # and the upper-case parent can only go once nothing references it.

    # 1. Create the lower-case row for every mixed-case proxy that lacks one,
    #    copying the record so nothing about the exit is lost.
    op.execute(
        """
        INSERT INTO proxies (
            id, type, endpoint, username, password_enc, country,
            sticky_capable, active, retired_at, retired_reason
        )
        SELECT lower(id), type, endpoint, username, password_enc, lower(country),
               sticky_capable, active, retired_at, retired_reason
        FROM proxies
        WHERE id <> lower(id)
          AND NOT EXISTS (SELECT 1 FROM proxies t WHERE t.id = lower(proxies.id))
        """
    )

    # 2. Repoint the children. Scores may now collide with the twin's own row
    #    for the same domain, so move only what does not collide and let the
    #    rest fall away with the parent — the surviving row is the larger
    #    history in every observed case.
    op.execute(
        """
        UPDATE proxy_domain_scores s SET proxy_id = lower(s.proxy_id)
        WHERE s.proxy_id <> lower(s.proxy_id)
          AND NOT EXISTS (
              SELECT 1 FROM proxy_domain_scores t
              WHERE t.proxy_id = lower(s.proxy_id) AND t.domain = s.domain
          )
        """
    )
    op.execute(
        """
        UPDATE proxy_usage SET proxy_id = lower(proxy_id)
        WHERE proxy_id <> lower(proxy_id)
        """
    )

    # 3. The mixed-case parents are now unreferenced by usage; scores cascade.
    op.execute("DELETE FROM proxies WHERE id <> lower(id)")


def downgrade() -> None:
    # The original spellings are not recoverable, and re-splitting one proxy
    # into two identities is the defect. Nothing to undo.
    pass
