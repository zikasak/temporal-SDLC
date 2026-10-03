"""Per-request state shared by the tools the model can call."""

import re
from dataclasses import dataclass, field

import psycopg

from app.documents import DocumentNotFound, get_generated_document, save_session_passage

DOCUMENT_URI = re.compile(r"document://([0-9a-f]{32})")
MAX_TOOL_CHARS = 8000


@dataclass
class ToolContext:
    session_id: str
    allowed_document_ids: set[str] = field(default_factory=set)

    async def authorized_document(self, uri: str) -> tuple[str, int | None] | None:
        match = DOCUMENT_URI.fullmatch(uri.strip())
        if match is None:
            return None
        document_id = match.group(1)
        if document_id in self.allowed_document_ids:
            return document_id, None
        try:
            document = await get_generated_document(document_id, self.session_id)
        except DocumentNotFound:
            return None
        return document_id, document["revision"]

    async def remember(self, document_id: str, revision: int | None, offset: int, text: str):
        # Model-created documents can change, so only uploaded text is remembered.
        if revision is not None:
            return
        try:
            await save_session_passage(self.session_id, document_id, offset, text)
        except psycopg.Error:
            pass
