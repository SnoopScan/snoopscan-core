"""Roll usage up per COST KEY, not per hardcoded column.

`usage_daily` has four tier columns — direct, proxied, browser, cached — and
the engine bills twelve keys. Search, map, extract, places, PDF pages and the
platform shortcuts never landed in any of them, so the members "By request
type" table priced four buckets against a total that included all twelve and
came to less than the figure printed above it. A customer could not account
for the difference on their own usage screen.

Adding seven more columns would fix today and break again the next time a
price is added — the desk can add a cost key without a deploy, and a column
cannot follow it. So the breakdown is a row per key instead: any key the
pricer returns rolls up here with no migration and no code change.

`usage_daily` keeps requests and credits, which is what the daily chart
draws. Its four tier columns stay for now so nothing reading them breaks;
they are a strict subset of what this table holds.

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_daily_costs (
            api_key_id text NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
            day        date NOT NULL,
            cost_key   text NOT NULL,
            requests   integer NOT NULL DEFAULT 0,
            credits    integer NOT NULL DEFAULT 0,
            PRIMARY KEY (api_key_id, day, cost_key)
        )
        """
    )
    # The summary filters by day across every key an owner holds.
    op.execute("CREATE INDEX IF NOT EXISTS usage_daily_costs_day_idx ON usage_daily_costs (day)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS usage_daily_costs")
