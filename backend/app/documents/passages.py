"""Passages the model retrieved earlier in a conversation."""

from datetime import datetime
from typing import Any

from app.db import connect


async def save_session_passage(
    session_id: str, document_id: str, offset: int, text: str
) -> None:
    if not text.strip():
        return
    async with await connect() as conn:
        await conn.execute(
            """
            INSERT INTO app.session_passages (session_id, document_id, "offset", text)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (session_id, document_id, "offset")
            DO UPDATE SET text = EXCLUDED.text, retrieved_at = now()
            """,
            (session_id, document_id, offset, text),
        )


async def copy_session_passages(
    source_session_id: str, target_session_id: str, retrieved_before: datetime
) -> None:
    async with await connect() as conn:
        await conn.execute(
            """
            INSERT INTO app.session_passages
                (session_id, document_id, "offset", text, retrieved_at)
            SELECT %s, document_id, "offset", text, retrieved_at
            FROM app.session_passages
            WHERE session_id = %s AND retrieved_at <= %s
            ON CONFLICT DO NOTHING
            """,
            (target_session_id, source_session_id, retrieved_before),
        )


async def list_session_passages(
    session_id: str, document_ids: set[str], max_chars: int
) -> list[dict[str, Any]]:
    """Return the most recently retrieved passages that fit in max_chars, in document order."""
    if not document_ids:
        return []
    async with await connect() as conn:
        cursor = await conn.execute(
            """
            SELECT p.document_id, p."offset", p.text
            FROM app.session_passages p
            JOIN app.documents d ON d.id = p.document_id
            WHERE p.session_id = %s AND p.document_id = ANY(%s::uuid[])
              AND d.expires_at > now()
            ORDER BY p.retrieved_at DESC
            """,
            (session_id, list(document_ids)),
        )
        rows = await cursor.fetchall()
    passages: list[dict[str, Any]] = []
    used = 0
    for row in rows:
        if used + len(row["text"]) > max_chars:
            continue
        used += len(row["text"])
        passages.append({"document_id": str(row["document_id"]).replace("-", ""),
                         "offset": row["offset"], "text": row["text"]})
    passages.sort(key=lambda item: (item["document_id"], item["offset"]))
    return passages
