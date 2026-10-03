"""Text documents the model creates and edits within a conversation."""

import hashlib
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from app.db import connect
from app.documents.errors import DocumentConflict, DocumentNotFound, DocumentValidationError
from app.documents.storage import new_expiry

logger = logging.getLogger(__name__)

MAX_GENERATED_BYTES = 256 * 1024
CONTENT_TYPES = {"md": "text/markdown", "yaml": "application/yaml", "yml": "application/yaml"}


def _generated_metadata(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": str(row["id"]).replace("-", ""),
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size_bytes": row["size_bytes"],
        "revision": row["revision"],
    }


def _generated_filename(filename: str) -> str:
    if (
        not filename
        or len(filename) > 255
        or filename != filename.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or not re.fullmatch(r"[^/\\]+\.(?:txt|md|py|ya?ml)", filename, re.IGNORECASE)
    ):
        raise DocumentValidationError("Use a single .txt, .md, .py, or .yaml filename.")
    return filename


def _generated_content(content: str) -> bytes:
    if "\x00" in content:
        raise DocumentValidationError("Document content cannot contain NUL characters.")
    try:
        encoded = content.encode("utf-8")
    except UnicodeError as exc:
        raise DocumentValidationError("Document content must be UTF-8 text.") from exc
    if len(encoded) > MAX_GENERATED_BYTES:
        raise DocumentValidationError("The generated document exceeds 256 KiB.")
    return encoded


async def list_generated_documents(session_id: str) -> list[dict[str, Any]]:
    async with await connect() as conn:
        cursor = await conn.execute(
            "SELECT id, filename, content_type, size_bytes, revision "
            "FROM app.documents WHERE origin = 'llm' AND session_id = %s "
            "AND expires_at > now() ORDER BY created_at, id",
            (session_id,),
        )
        rows = await cursor.fetchall()
    return [_generated_metadata(row) for row in rows]


async def touch_generated_documents(session_id: str) -> None:
    async with await connect() as conn:
        await conn.execute(
            "UPDATE app.documents SET expires_at = %s "
            "WHERE origin = 'llm' AND session_id = %s AND expires_at > now()",
            (new_expiry(), session_id),
        )


async def copy_generated_documents(
    source_session_id: str, target_session_id: str, created_before: datetime
) -> int:
    """Give a forked conversation its own copy of the documents that existed
    at the fork, so edits in one branch don't change the other."""
    async with await connect() as conn:
        cursor = await conn.execute(
            """
            WITH source AS (
                SELECT gen_random_uuid() AS copy_id, *
                FROM app.documents
                WHERE origin = 'llm' AND session_id = %s AND expires_at > now()
                  AND created_at <= %s
            )
            INSERT INTO app.documents
                (id, filename, content_type, size_bytes, sha256, storage_name,
                 parse_status, parsed_text, origin, session_id, revision,
                 created_at, expires_at)
            SELECT copy_id, filename, content_type, size_bytes, sha256,
                   replace(copy_id::text, '-', ''), parse_status, parsed_text,
                   origin, %s, revision, created_at, %s
            FROM source
            """,
            (source_session_id, created_before, target_session_id, new_expiry()),
        )
        copied = cursor.rowcount
    logger.info("Copied %d generated documents from session %s to %s",
                copied, source_session_id, target_session_id)
    return copied


async def get_generated_document(document_id: str, session_id: str) -> dict[str, Any]:
    async with await connect() as conn:
        cursor = await conn.execute(
            "SELECT * FROM app.documents WHERE id = %s AND origin = 'llm' "
            "AND session_id = %s AND expires_at > now()",
            (document_id, session_id),
        )
        row = await cursor.fetchone()
    if row is None:
        raise DocumentNotFound("The generated document is not available in this session.")
    return row


async def create_generated_document(
    session_id: str, filename: str, content: str
) -> dict[str, Any]:
    filename = _generated_filename(filename)
    encoded = _generated_content(content)
    document_id = uuid.uuid4().hex
    content_type = CONTENT_TYPES.get(filename.rsplit(".", 1)[-1].lower(), "text/plain")
    async with await connect() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO app.documents
                (id, filename, content_type, size_bytes, sha256, storage_name,
                 parse_status, parsed_text, origin, session_id, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'parsed', %s, 'llm', %s, %s)
            RETURNING id, filename, content_type, size_bytes, revision
            """,
            (document_id, filename, content_type, len(encoded),
             hashlib.sha256(encoded).hexdigest(), document_id, content,
             session_id, new_expiry()),
        )
        row = await cursor.fetchone()
    logger.info("Created generated document %s %r for session %s (%d bytes)",
                document_id, filename, session_id, len(encoded))
    return _generated_metadata(row)


async def edit_generated_document(
    session_id: str,
    document_id: str,
    expected_revision: int,
    edit_mode: str,
    content: str | None = None,
    old_text: str | None = None,
    new_text: str | None = None,
) -> dict[str, Any]:
    async with await connect() as conn:
        async with conn.transaction():
            cursor = await conn.execute(
                "SELECT * FROM app.documents WHERE id = %s AND origin = 'llm' "
                "AND session_id = %s AND expires_at > now() FOR UPDATE",
                (document_id, session_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise DocumentNotFound("The generated document is not available in this session.")
            if row["revision"] != expected_revision:
                raise DocumentConflict(f"Revision conflict. Current revision: {row['revision']}.")
            if edit_mode == "replace":
                if content is None:
                    raise DocumentValidationError("A replacement edit needs content.")
                updated = content
            elif edit_mode == "replace_text":
                if not old_text or new_text is None:
                    raise DocumentValidationError("A targeted edit needs old_text and new_text.")
                if row["parsed_text"].count(old_text) != 1:
                    raise DocumentConflict("old_text must occur exactly once in the document.")
                updated = row["parsed_text"].replace(old_text, new_text, 1)
            else:
                raise DocumentValidationError("Use replace or replace_text for edit_mode.")
            encoded = _generated_content(updated)
            cursor = await conn.execute(
                """
                UPDATE app.documents SET parsed_text = %s, size_bytes = %s,
                    sha256 = %s, revision = revision + 1, expires_at = %s
                WHERE id = %s
                RETURNING id, filename, content_type, size_bytes, revision
                """,
                (updated, len(encoded), hashlib.sha256(encoded).hexdigest(),
                 new_expiry(), document_id),
            )
            changed = await cursor.fetchone()
    logger.info("Edited generated document %s (%s, revision %d)",
                document_id, edit_mode, changed["revision"])
    return _generated_metadata(changed)


async def delete_generated_document(
    session_id: str, document_id: str, expected_revision: int
) -> None:
    async with await connect() as conn:
        async with conn.transaction():
            cursor = await conn.execute(
                "SELECT revision FROM app.documents WHERE id = %s AND origin = 'llm' "
                "AND session_id = %s AND expires_at > now() FOR UPDATE",
                (document_id, session_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise DocumentNotFound("The generated document is not available in this session.")
            if row["revision"] != expected_revision:
                raise DocumentConflict(f"Revision conflict. Current revision: {row['revision']}.")
            await conn.execute("DELETE FROM app.documents WHERE id = %s", (document_id,))
    logger.info("Deleted generated document %s from session %s", document_id, session_id)
