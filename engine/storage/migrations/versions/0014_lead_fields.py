"""The fields a lead buyer actually receives.

A lead list is sold on eleven columns: first and last name, title, email,
LinkedIn profile, business name, website, phone, location, headcount, industry,
revenue. We held three of them — email, business name, website — so an export
was a list of addresses, not a list of leads.

Most of the rest is on the company's own site and needs no third-party key:
schema.org `Organization` and `PostalAddress` carry phone and address, team and
about pages carry names and titles, and `numberOfEmployees` is a schema.org
field companies publish about themselves. That matters more than convenience —
the whole point is that a customer needs no credentials of their own.

Person fields live on `contacts` because they describe a person; firmographics
live on `companies` because they describe the business and are the same for
every contact at it. Splitting them the other way would store the headcount
once per employee and let two rows disagree about it.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS first_name   text,
            ADD COLUMN IF NOT EXISTS last_name    text,
            ADD COLUMN IF NOT EXISTS full_name    text,
            ADD COLUMN IF NOT EXISTS title        text,
            ADD COLUMN IF NOT EXISTS linkedin_url text,
            ADD COLUMN IF NOT EXISTS phone        text
        """
    )
    op.execute(
        """
        ALTER TABLE companies
            ADD COLUMN IF NOT EXISTS phone         text,
            ADD COLUMN IF NOT EXISTS street        text,
            ADD COLUMN IF NOT EXISTS city          text,
            ADD COLUMN IF NOT EXISTS region        text,
            ADD COLUMN IF NOT EXISTS postal_code   text,
            ADD COLUMN IF NOT EXISTS country       text,
            ADD COLUMN IF NOT EXISTS linkedin_url  text,
            ADD COLUMN IF NOT EXISTS headcount     integer,
            ADD COLUMN IF NOT EXISTS headcount_raw text,
            ADD COLUMN IF NOT EXISTS industry      text,
            ADD COLUMN IF NOT EXISTS revenue_raw   text,
            ADD COLUMN IF NOT EXISTS founded_year  integer,
            ADD COLUMN IF NOT EXISTS description   text
        """
    )
    # Firmographics are enriched separately from contact discovery, so the
    # pipeline needs to find the companies it has not enriched yet.
    op.execute("ALTER TABLE companies ADD COLUMN IF NOT EXISTS enriched_at timestamptz")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_companies_unenriched "
        "ON companies (created_at) WHERE enriched_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_companies_unenriched")
    for column in ("first_name", "last_name", "full_name", "title", "linkedin_url", "phone"):
        op.execute(f"ALTER TABLE contacts DROP COLUMN IF EXISTS {column}")
    for column in (
        "phone",
        "street",
        "city",
        "region",
        "postal_code",
        "country",
        "linkedin_url",
        "headcount",
        "headcount_raw",
        "industry",
        "revenue_raw",
        "founded_year",
        "description",
        "enriched_at",
    ):
        op.execute(f"ALTER TABLE companies DROP COLUMN IF EXISTS {column}")
