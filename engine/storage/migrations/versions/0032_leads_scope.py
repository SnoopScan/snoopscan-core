"""Grant the new `leads` scope to keys that already exist.

`require_api_key` denies any /v1 path whose first segment is not a scope the
key holds, so a new route is unreachable until its scope is both mapped and
granted. 0028 did this for `domain`; this does it for `leads`.

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE api_keys SET scopes = array_append(scopes, 'leads') "
        "WHERE active AND NOT ('leads' = ANY(scopes))"
    )


def downgrade() -> None:
    op.execute("UPDATE api_keys SET scopes = array_remove(scopes, 'leads')")
