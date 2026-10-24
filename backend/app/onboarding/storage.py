"""The repository and branch of each onboarding, for the one-at-a-time rule."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg

from app.db import connect


async def ensure_schema() -> None:
    async with await connect() as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS app")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.onboarding_sessions (
                session_id text PRIMARY KEY,
                full_name text NOT NULL,
                branch text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS onboarding_sessions_branch
            ON app.onboarding_sessions (lower(full_name), branch)
        """)


@asynccontextmanager
async def branch_lock(full_name: str, branch: str) -> AsyncIterator[psycopg.AsyncConnection]:
    """Hold the repository branch's lock for one transaction, so that two onboardings of
    it never start at once."""
    async with await connect() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                               (f"onboarding:{full_name.lower()}:{branch}",))
            yield conn


async def sessions_of(conn: psycopg.AsyncConnection, full_name: str, branch: str) -> list[str]:
    cursor = await conn.execute(
        "SELECT session_id FROM app.onboarding_sessions "
        "WHERE lower(full_name) = lower(%s) AND branch = %s ORDER BY created_at DESC",
        (full_name, branch))
    return [row["session_id"] for row in await cursor.fetchall()]


async def add(conn: psycopg.AsyncConnection, session_id: str, full_name: str,
              branch: str) -> None:
    await conn.execute(
        "INSERT INTO app.onboarding_sessions (session_id, full_name, branch) "
        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (session_id, full_name, branch))


async def record(session_id: str, full_name: str, branch: str) -> None:
    async with await connect() as conn:
        await add(conn, session_id, full_name, branch)
