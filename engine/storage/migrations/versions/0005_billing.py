"""Billing: key ownership and balances, credit costs, usage events + daily rollup.

Revision ID: 0005_billing
Revises: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0005_billing"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The app owns customers; the engine enforces at request time. owner_ref
    # is the app's identity uuid, credits_remaining the balance it grants.
    # One balance per customer, shared by every key they hold. The app grants
    # to the owner; the engine decrements the owner on every metered response.
    op.execute(
        """
        CREATE TABLE owners (
            owner_ref          text PRIMARY KEY,
            credits_remaining  bigint NOT NULL DEFAULT 0,
            concurrency        integer NOT NULL DEFAULT 5,
            suspended          boolean NOT NULL DEFAULT false,
            updated_at         timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE api_keys ADD COLUMN owner_ref text REFERENCES owners(owner_ref)")
    op.execute("ALTER TABLE api_keys ADD COLUMN prefix text")
    op.execute("CREATE INDEX idx_api_keys_owner ON api_keys (owner_ref)")

    op.execute(
        """
        CREATE TABLE credit_costs (
            key         text PRIMARY KEY,
            credits     integer NOT NULL,
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        INSERT INTO credit_costs (key, credits) VALUES
            ('direct', 1), ('proxied', 2), ('browser', 5), ('pdf_page', 1), ('cached', 0)
        """
    )

    op.execute(
        """
        CREATE TABLE usage_events (
            id           bigserial PRIMARY KEY,
            api_key_id   text NOT NULL REFERENCES api_keys(id),
            job_id       text,
            endpoint     text NOT NULL,
            host         text,
            tier         text,
            proxy_bytes  bigint NOT NULL DEFAULT 0,
            cached       boolean NOT NULL DEFAULT false,
            pdf_pages    integer NOT NULL DEFAULT 0,
            credits      integer NOT NULL,
            recorded_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_usage_events_key_time ON usage_events (api_key_id, recorded_at DESC)"
    )

    # The rollup a dashboard reads. Never the raw events.
    op.execute(
        """
        CREATE TABLE usage_daily (
            api_key_id   text NOT NULL REFERENCES api_keys(id),
            day          date NOT NULL,
            requests     integer NOT NULL DEFAULT 0,
            credits      bigint NOT NULL DEFAULT 0,
            direct       integer NOT NULL DEFAULT 0,
            proxied      integer NOT NULL DEFAULT 0,
            browser      integer NOT NULL DEFAULT 0,
            cached       integer NOT NULL DEFAULT 0,
            PRIMARY KEY (api_key_id, day)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE usage_daily")
    op.execute("DROP TABLE usage_events")
    op.execute("DROP TABLE credit_costs")
    op.execute("DROP INDEX IF EXISTS idx_api_keys_owner")
    op.execute("ALTER TABLE api_keys DROP COLUMN prefix")
    op.execute("ALTER TABLE api_keys DROP COLUMN owner_ref")
    op.execute("DROP TABLE owners")
