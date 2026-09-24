"""Postgres access (principle P4: stateless workers, stateful Postgres).

asyncpg directly rather than an ORM: the queries here are few, specific, and
several of them (the frontier claim in particular) must be exactly the SQL the
spec mandates rather than whatever an ORM generates.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from engine.settings import settings

_pool: asyncpg.Pool | None = None

# A runaway query must not hold a connection open indefinitely.
STATEMENT_TIMEOUT = "30s"


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
    # jsonb in and out as Python objects rather than strings.
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.asyncpg_dsn,
            min_size=2,
            max_size=10,
            init=_init_connection,
            command_timeout=60,
        )
    assert _pool is not None
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def connection() -> AsyncIterator[asyncpg.Connection]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        yield conn


def _scrub(value: Any) -> Any:
    """Strip NUL bytes out of anything on its way to Postgres.

    Postgres text cannot hold 0x00 and asyncpg raises
    CharacterNotInRepertoireError, which reached the caller as a 500 — AFTER
    the fetch had succeeded and been paid for. Real pages do carry stray NULs
    (an X post did, 6 Sep 2026); a NUL is never content, only a parsing
    artefact, so dropping it loses nothing.

    Done HERE, at the one place every query passes through, because the
    alternative is remembering it at each of the callers that write page text,
    job payloads, monitor snapshots and metadata — and one forgotten caller is
    a 500 on a page we already charged for.
    """
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, (list, tuple)):
        cleaned = [_scrub(v) for v in value]
        return type(value)(cleaned) if isinstance(value, tuple) else cleaned
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def _scrubbed(args: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(_scrub(a) for a in args)


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    async with connection() as conn:
        rows: list[asyncpg.Record] = await conn.fetch(query, *_scrubbed(args))
        return rows


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    async with connection() as conn:
        return await conn.fetchrow(query, *_scrubbed(args))


async def fetchval(query: str, *args: Any) -> Any:
    async with connection() as conn:
        return await conn.fetchval(query, *_scrubbed(args))


async def execute(query: str, *args: Any) -> str:
    async with connection() as conn:
        status: str = await conn.execute(query, *_scrubbed(args))
        return status


async def executemany(query: str, args: list[tuple[Any, ...]]) -> None:
    """One statement, many rows, one round trip.

    Added for the link graph, where a single page contributes tens of rows and
    a loop of `execute` would be tens of round trips per scrape. Arguments are
    scrubbed the same way as everywhere else: a NUL byte reaching Postgres is
    an error, and page content is full of them.
    """
    async with connection() as conn:
        await conn.executemany(query, [_scrubbed(row) for row in args])


async def healthy() -> bool:
    """Readiness probe. Never raises — a failure pulls the instance from
    rotation rather than crashing it."""
    try:
        async with connection() as conn:
            value = await conn.fetchval("SELECT 1")
            return bool(value == 1)
    except (asyncpg.PostgresError, OSError):
        return False
