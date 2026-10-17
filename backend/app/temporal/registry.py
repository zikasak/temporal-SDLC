"""IDs of the sessions and chat requests this instance started.

Temporal holds their state; this record lets the API tell an unknown ID (404)
from a known one whose state Temporal cannot serve right now (503)."""

import logging

import psycopg

from app.db import connect

logger = logging.getLogger(__name__)


async def ensure_schema() -> None:
    async with await connect() as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS app")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.workflow_registry (
                workflow_id text PRIMARY KEY,
                created_at timestamptz NOT NULL DEFAULT now()
            )
        """)


async def register(workflow_id: str) -> None:
    """Record a started session or request; a storage failure only loses the record."""
    try:
        async with await connect() as conn:
            await conn.execute(
                "INSERT INTO app.workflow_registry (workflow_id) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                (workflow_id,),
            )
    except (OSError, psycopg.Error):
        logger.exception("Could not record workflow %s", workflow_id)


async def is_registered(workflow_id: str) -> bool:
    async with await connect() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM app.workflow_registry WHERE workflow_id = %s", (workflow_id,)
        )
        return await cursor.fetchone() is not None
