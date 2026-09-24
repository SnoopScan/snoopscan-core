"""Remember which domains a proxy provider will not carry.

Some providers refuse whole categories of target — finance, government,
social, streaming, some mail — unless the account has completed their ID
verification. They answer the tunnel with a 403 and carry everything else:
a payments site and a government site refused at the tunnel, example.com and
httpbin carried.

The engine had two wrong readings of that 403, one per HTTP client. httpx's
"ProxyError: 403" matched the provider-failure pattern, so three refused
domains in a row benched the provider for ALL traffic. curl_cffi's "CONNECT
tunnel failed, response 403" matched nothing, so the provider was scored as
working and sent the same refused domain on every request.

It is neither. It is a fact about one provider and one domain, and it is kept
here so every worker and every restart routes around it. Refusals expire after
30 days so a provider that changes its policy is eventually tried again.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_provider_refusals (
            provider_id text NOT NULL,
            domain      text NOT NULL,
            refused_at  timestamptz NOT NULL DEFAULT now(),
            expires_at  timestamptz NOT NULL,
            refusals    integer NOT NULL DEFAULT 1,
            PRIMARY KEY (provider_id, domain)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS proxy_provider_refusals")
