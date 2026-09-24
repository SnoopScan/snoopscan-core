"""Grant the new `domain` scope to keys that already exist.

`require_api_key` denies any /v1 path whose first segment is not mapped to a
scope the key HOLDS, and an unmapped path denies outright — that is deliberate,
so a new route is unreachable until it is named. The other side of it is that
adding a route also has to grant its scope to the keys already issued, or
every existing customer gets a 403 on a capability they were just given.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE api_keys
        SET scopes = array_append(scopes, 'domain')
        WHERE active AND NOT ('domain' = ANY(scopes))
        """
    )


def downgrade() -> None:
    op.execute("UPDATE api_keys SET scopes = array_remove(scopes, 'domain')")
