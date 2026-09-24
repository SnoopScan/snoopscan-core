"""Rename the `leads` scope to `company`.

`leads` named our business rather than what the endpoint returns; the category
calls this `organization` (Apollo) or `company`. Nothing external depends on it
yet, so the old scope is removed rather than kept as an alias.

Revision ID: 0033
Revises: 0032
"""

from __future__ import annotations

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE api_keys SET scopes = array_append(scopes, 'company') "
        "WHERE active AND NOT ('company' = ANY(scopes))"
    )
    op.execute("UPDATE api_keys SET scopes = array_remove(scopes, 'leads')")


def downgrade() -> None:
    op.execute(
        "UPDATE api_keys SET scopes = array_append(scopes, 'leads') "
        "WHERE active AND NOT ('leads' = ANY(scopes))"
    )
    op.execute("UPDATE api_keys SET scopes = array_remove(scopes, 'company')")
