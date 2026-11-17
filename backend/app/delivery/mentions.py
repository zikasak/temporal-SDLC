"""The durable history of the mentions of the GitHub App on a delivery's pull request.

The delivery workflow carries only the mentions it is still working on, without their
text, so that its state stays bounded however many comments its pull request receives;
every mention, with its full text and how it ended, is kept here and shown from here."""

from typing import Any

from psycopg.types.json import Jsonb

from app.db import connect
from app.github.models import PullRequestMention


async def ensure_schema() -> None:
    async with await connect() as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS app")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.delivery_mentions (
                session_id text NOT NULL,
                comment_type text NOT NULL,
                comment_id bigint NOT NULL,
                -- The order the delivery received its mentions in.
                position bigserial NOT NULL,
                mention jsonb NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (session_id, comment_type, comment_id)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS delivery_mentions_session
                ON app.delivery_mentions (session_id, position)
        """)
        # Which recording operation stored the mention, so that a retry of that same
        # operation, after its write committed, finds its own row instead of a duplicate.
        await conn.execute("""
            ALTER TABLE app.delivery_mentions ADD COLUMN IF NOT EXISTS recorded_by text
        """)


async def add(session_id: str, mention: PullRequestMention, recording: str | None = None) -> bool:
    """Record a mention the delivery received, with its full text; false when the
    delivery already has it from another recording. `recording` identifies the recording
    operation: a retry of the same operation whose earlier attempt committed finds its own
    row and answers true again, so the mention is handled exactly once."""
    async with await connect() as conn:
        cursor = await conn.execute("""
            INSERT INTO app.delivery_mentions
                (session_id, comment_type, comment_id, mention, recorded_by)
            VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING comment_id
        """, (session_id, mention['comment_type'], mention['comment_id'], Jsonb(mention),
              recording))
        if await cursor.fetchone() is not None:
            return True
        if not recording:
            return False
        cursor = await conn.execute("""
            SELECT recorded_by FROM app.delivery_mentions
            WHERE session_id = %s AND comment_type = %s AND comment_id = %s
        """, (session_id, mention['comment_type'], mention['comment_id']))
        row = await cursor.fetchone()
        return row is not None and row['recorded_by'] == recording


async def recording(session_id: str, comment_type: str, comment_id: int) -> str | None:
    """Which recording stored the mention: None while the delivery has not recorded it,
    an empty string when it was stored without a recording."""
    async with await connect() as conn:
        cursor = await conn.execute("""
            SELECT recorded_by FROM app.delivery_mentions
            WHERE session_id = %s AND comment_type = %s AND comment_id = %s
        """, (session_id, comment_type, comment_id))
        row = await cursor.fetchone()
    return None if row is None else (row['recorded_by'] or '')


async def save(session_id: str, mention: PullRequestMention) -> None:
    """Record how far a mention has come. Its text, which the workflow does not carry,
    stays as it was recorded."""
    async with await connect() as conn:
        await conn.execute("""
            INSERT INTO app.delivery_mentions (session_id, comment_type, comment_id, mention)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (session_id, comment_type, comment_id) DO UPDATE SET
                mention = CASE WHEN EXCLUDED.mention ? 'body' THEN EXCLUDED.mention
                          ELSE EXCLUDED.mention || jsonb_build_object(
                              'body', COALESCE(delivery_mentions.mention -> 'body',
                                               to_jsonb(''::text)))
                          END,
                updated_at = now()
        """, (session_id, mention['comment_type'], mention['comment_id'], Jsonb(mention)))


async def body(session_id: str, comment_type: str, comment_id: int) -> str | None:
    """The full text of a recorded mention."""
    async with await connect() as conn:
        cursor = await conn.execute("""
            SELECT mention ->> 'body' AS body FROM app.delivery_mentions
            WHERE session_id = %s AND comment_type = %s AND comment_id = %s
        """, (session_id, comment_type, comment_id))
        row = await cursor.fetchone()
    return row['body'] if row else None


async def history(session_id: str) -> list[dict[str, Any]]:
    """Every mention the delivery received, oldest first."""
    async with await connect() as conn:
        cursor = await conn.execute("""
            SELECT mention FROM app.delivery_mentions WHERE session_id = %s ORDER BY position
        """, (session_id,))
        rows = await cursor.fetchall()
    return [dict(row['mention']) for row in rows]
