"""Alembic environment.

Runs through asyncpg rather than psycopg2 deliberately: psycopg2 is LGPL and
would fail the licence gate (constraint C2). Alembic supports an async engine,
so there is no reason to add a copyleft driver just for migrations.

The DSN comes from settings, which reads the environment only (constraint C3).
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from engine.settings import settings

config = context.config

_ASYNC_DSN = settings.database_url.replace("postgresql://", "postgresql+asyncpg://")
config.set_main_option("sqlalchemy.url", _ASYNC_DSN)


def run_migrations_offline() -> None:
    context.configure(
        url=_ASYNC_DSN,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
    )
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
