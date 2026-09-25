"""What each proxy provider charges per GB, and an ISP exit type.

Priority alone cannot say "cheapest first": four residential providers at one
priority shared traffic evenly and the dearest carried the most bytes. With a
price on each provider the router takes the cheapest healthy one within a
priority tier, and the spend report prices the ledger with the same figure.
NULL means not priced, and the type's list-price estimate stands in.

`isp` (static residential) sits between datacenter and residential on price,
and joins the proxy_type enum so the desk can register one.
"""

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE proxy_providers
            ADD COLUMN cost_per_gb numeric(10, 4)
            CHECK (cost_per_gb IS NULL OR cost_per_gb >= 0)
        """
    )
    # Postgres 12+ allows this inside a transaction as long as the new value is
    # not USED in the same one, and nothing here uses it.
    op.execute("ALTER TYPE proxy_type ADD VALUE IF NOT EXISTS 'isp' AFTER 'datacenter'")


def downgrade() -> None:
    # An enum value cannot be dropped in place; leaving `isp` is harmless.
    op.execute("ALTER TABLE proxy_providers DROP COLUMN cost_per_gb")
