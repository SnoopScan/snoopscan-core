"""Remember a consent cookie the engine harvested for itself.

`site_rules.yaml` ships the cookie that dismisses a consent wall (Google's
`SOCS`). The vendor rotates it, and when the shipped value dies the engine can
now click "Accept all" over HTTP and harvest a fresh one (fetch/consent.py).
That value has to outlive the process that found it — otherwise every worker
re-harvests on its first Google request and a restart is a burst of consent
POSTs. One row per (registrable domain, cookie name); the newest wins over the
YAML default.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS site_cookies (
            rule_host    TEXT        NOT NULL,
            name         TEXT        NOT NULL,
            value        TEXT        NOT NULL,
            harvested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (rule_host, name)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS site_cookies")
