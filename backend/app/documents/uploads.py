"""Uploaded originals on disk, their metadata, and extracted text."""

import hashlib
import logging
import os
import uuid
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from starlette.datastructures import UploadFile

from app.asyncio_utils import async_context, run_sync
from app.config import DOCUMENT_STORAGE_DIR
from app.conversations.models import AttachmentMetadata
from app.db import connect
from app.documents.errors import DocumentNotFound, DocumentParseError, DocumentTooLarge
from app.documents.parsers import document_type
from app.documents.storage import new_expiry, storage_path

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


async def store_original(upload: UploadFile) -> AttachmentMetadata:
    filename = ((upload.filename or "").replace("\\", "/")).split("/")[-1]
    if not filename or len(filename) > 255:
        raise DocumentParseError("Choose a file with a valid filename.")
    content_type, _ = document_type(filename)
    document_id = uuid.uuid4().hex
    storage_name = document_id
    temporary_path = storage_path(f"{storage_name}.upload")
    final_path = storage_path(storage_name)
    await run_sync(DOCUMENT_STORAGE_DIR.mkdir, parents=True, exist_ok=True)
    size = 0
    digest = hashlib.sha256()

    try:
        async with async_context(temporary_path.open, "xb") as target:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise DocumentTooLarge("The file exceeds the 20 MiB upload limit.")
                digest.update(chunk)
                await run_sync(target.write, chunk)
        if size == 0:
            raise DocumentParseError("The attached file is empty.")
        await run_sync(_validate_upload, temporary_path, filename)
        await run_sync(os.replace, temporary_path, final_path)
        async with await connect() as conn:
            await conn.execute(
                """
                INSERT INTO app.documents
                    (id, filename, content_type, size_bytes, sha256, storage_name, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (document_id, filename, content_type, size, digest.hexdigest(),
                 storage_name, new_expiry()),
            )
    except BaseException as exc:
        logger.warning("Could not store upload %r: %s", filename, exc)
        await run_sync(temporary_path.unlink, missing_ok=True)
        await run_sync(final_path.unlink, missing_ok=True)
        raise
    finally:
        await upload.close()
    logger.info("Stored upload %s (%s, %d bytes)", document_id, content_type, size)

    return {
        "document_id": document_id,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": size,
    }


def _validate_upload(temporary_path: Path, filename: str) -> None:
    with temporary_path.open("rb") as source:
        header = source.read(8)
    if filename.lower().endswith(".pdf") and not header.startswith(b"%PDF-"):
        raise DocumentParseError("The attached file is not a PDF.")
    if filename.lower().endswith(".docx"):
        try:
            with ZipFile(temporary_path) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise DocumentParseError("The attached file is not a DOCX document.")
        except BadZipFile as exc:
            raise DocumentParseError("The attached file is not a DOCX document.") from exc


async def get_document(document_id: str) -> dict[str, Any]:
    async with await connect() as conn:
        cursor = await conn.execute(
            "SELECT * FROM app.documents WHERE id = %s AND expires_at > now()",
            (document_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise DocumentNotFound("The attached document is no longer available.")
    return row


async def touch_documents(document_ids: set[str]) -> None:
    if not document_ids:
        return
    expires_at = new_expiry()
    async with await connect() as conn:
        for document_id in document_ids:
            cursor = await conn.execute(
                """
                UPDATE app.documents SET expires_at = %s
                WHERE id = %s AND expires_at > now()
                """,
                (expires_at, document_id),
            )
            if cursor.rowcount != 1:
                raise DocumentNotFound("An attached document is no longer available.")


async def parse_stored_document(document_id: str) -> None:
    error: DocumentParseError | None = None
    async with await connect() as conn:
        async with conn.transaction():
            cursor = await conn.execute(
                "SELECT filename, storage_name, parse_status FROM app.documents "
                "WHERE id = %s AND expires_at > now() FOR UPDATE",
                (document_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise DocumentNotFound("The attached document is no longer available.")
            if row["parse_status"] == "parsed":
                return
            _, parser = document_type(row["filename"])
            try:
                text = await run_sync(parser, storage_path(row["storage_name"]))
            except DocumentParseError as exc:
                error = exc
                await conn.execute(
                    "UPDATE app.documents SET parse_status = 'failed', parse_error = %s "
                    "WHERE id = %s",
                    (str(exc), document_id),
                )
            except Exception:
                logger.exception("Parser crashed on document %s", document_id)
                error = DocumentParseError("The document could not be parsed.")
                await conn.execute(
                    "UPDATE app.documents SET parse_status = 'failed', parse_error = %s "
                    "WHERE id = %s",
                    (str(error), document_id),
                )
            else:
                await conn.execute(
                    """
                    UPDATE app.documents
                    SET parse_status = 'parsed', parsed_text = %s, parse_error = NULL
                    WHERE id = %s
                    """,
                    (text, document_id),
                )
                logger.info("Parsed document %s (%d characters)", document_id, len(text))
    if error:
        raise error


async def read_document_text(document_id: str, offset: int, limit: int) -> tuple[str, int]:
    async with await connect() as conn:
        cursor = await conn.execute(
            """
            SELECT substring(parsed_text FROM %s FOR %s) AS text,
                   length(parsed_text) AS total
            FROM app.documents
            WHERE id = %s AND parse_status = 'parsed' AND expires_at > now()
            """,
            (offset + 1, limit, document_id),
        )
        row = await cursor.fetchone()
    if row is None:
        raise DocumentNotFound("The parsed document is not available.")
    return row["text"] or "", row["total"]


async def delete_document(document_id: str) -> None:
    async with await connect() as conn:
        cursor = await conn.execute(
            "DELETE FROM app.documents WHERE id = %s RETURNING storage_name",
            (document_id,),
        )
        row = await cursor.fetchone()
    if row:
        await run_sync(storage_path(row["storage_name"]).unlink, missing_ok=True)
        logger.info("Deleted document %s", document_id)


async def cleanup_expired_documents() -> None:
    async with await connect() as conn:
        async with conn.transaction():
            cursor = await conn.execute(
                "SELECT id, storage_name FROM app.documents "
                "WHERE expires_at <= now() FOR UPDATE SKIP LOCKED"
            )
            rows = await cursor.fetchall()
            for row in rows:
                await run_sync(storage_path(row["storage_name"]).unlink, missing_ok=True)
                await conn.execute(
                    "DELETE FROM app.documents WHERE id = %s", (row["id"],)
                )
    if rows:
        logger.info("Removed %d expired documents", len(rows))


def original_path(document: dict[str, Any]) -> Path:
    return storage_path(document["storage_name"])
