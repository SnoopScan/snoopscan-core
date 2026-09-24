"""Proxy providers: the vendor registry the desk edits, replacing env-only config.

Revision ID: 0006
Revises: 0005_billing
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE proxy_providers (
            id                        text PRIMARY KEY,
            name                      text NOT NULL,
            type                      proxy_type NOT NULL,
            host                      text NOT NULL,
            port                      integer NOT NULL,
            username                  text NOT NULL,
            password_enc              text NOT NULL,
            country                   text,
            username_template         text NOT NULL DEFAULT '{username}',
            password_template         text NOT NULL DEFAULT '{password}_country-{country}',
            password_sticky_template  text NOT NULL
                DEFAULT '{password}_country-{country}_session-{session}_lifetime-{lifetime}m',
            sticky_lifetime_minutes   integer NOT NULL DEFAULT 10,
            enabled                   boolean NOT NULL DEFAULT true,
            priority                  integer NOT NULL DEFAULT 100,
            created_at                timestamptz NOT NULL DEFAULT now(),
            updated_at                timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_proxy_providers_pick ON proxy_providers (type, enabled, priority)")


def downgrade() -> None:
    op.execute("DROP TABLE proxy_providers")
