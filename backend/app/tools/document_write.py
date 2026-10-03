"""Tools that create, edit, replace, and delete model-created documents."""

import json
import logging
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool

from app.documents import (
    DocumentConflict,
    DocumentNotFound,
    DocumentValidationError,
    create_generated_document,
    delete_generated_document,
    edit_generated_document,
)
from app.tools.context import DOCUMENT_URI, ToolContext

logger = logging.getLogger(__name__)

DOCUMENT_ACTIONS = {
    "create_document": ("created", "Created"),
    "edit_document": ("edited", "Edited"),
    "replace_document": ("replaced", "Replaced"),
    "delete_document": ("deleted", "Deleted"),
}


def summarize_tool_results(messages: list[Any]) -> str | None:
    outcomes: list[str] = []
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name not in DOCUMENT_ACTIONS:
            continue
        expected_action, verb = DOCUMENT_ACTIONS[message.name]
        content = str(message.text).strip()
        try:
            result = json.loads(content)
        except (ValueError, TypeError):
            result = None
        if isinstance(result, dict) and result.get("action") == expected_action:
            uri = result.get("uri")
            if not isinstance(uri, str):
                continue
            filename = result.get("filename")
            label = f"{filename} ({uri})" if isinstance(filename, str) else uri
            outcomes.append(f"{verb} {label}.")
        elif content.startswith("Document operation failed: "):
            outcomes.append(f"Could not {message.name.removesuffix('_document')} document: "
                            f"{content.removeprefix('Document operation failed: ')}")
        elif message.status == "error":
            outcomes.append(f"Could not {message.name.removesuffix('_document')} document: "
                            f"{content or 'The tool call failed.'}")
    return "\n".join(outcomes) if outcomes else None


def build(ctx: ToolContext) -> list[BaseTool]:
    session_id = ctx.session_id

    def mutation_target(uri: str, expected_revision: int) -> str:
        match = DOCUMENT_URI.fullmatch(uri.strip())
        if match is None:
            raise DocumentValidationError("Use a valid document:// URI.")
        if expected_revision < 1:
            raise DocumentValidationError("Provide a positive expected_revision.")
        return match.group(1)

    def mutation_result(action: str, document: dict[str, object]) -> str:
        return json.dumps(
            {"action": action, "uri": f"document://{document['document_id']}",
             **document}, ensure_ascii=False
        )

    @tool
    async def create_document(filename: str, content: str) -> str:
        """Create a UTF-8 .txt or .md document in this conversation, up to 256 KiB."""
        try:
            document = await create_generated_document(session_id, filename, content)
            return mutation_result("created", document)
        except (DocumentNotFound, DocumentConflict, DocumentValidationError) as exc:
            logger.warning("Document operation failed: %s", exc)
            return f"Document operation failed: {exc}"

    @tool
    async def edit_document(
        uri: str, expected_revision: int, old_text: str, new_text: str
    ) -> str:
        """Replace one exact text match in a model-created document in this conversation.

        Supply the current revision from read_document. old_text must occur
        exactly once. Uploaded documents cannot be edited.
        """
        try:
            document_id = mutation_target(uri, expected_revision)
            document = await edit_generated_document(
                session_id, document_id, expected_revision, "replace_text",
                old_text=old_text, new_text=new_text,
            )
            return mutation_result("edited", document)
        except (DocumentNotFound, DocumentConflict, DocumentValidationError) as exc:
            logger.warning("Document operation failed: %s", exc)
            return f"Document operation failed: {exc}"

    @tool
    async def replace_document(uri: str, expected_revision: int, content: str) -> str:
        """Replace all content of a model-created text document in this conversation.

        Supply the current revision from read_document. Uploaded documents
        cannot be replaced.
        """
        try:
            document_id = mutation_target(uri, expected_revision)
            document = await edit_generated_document(
                session_id, document_id, expected_revision, "replace", content=content,
            )
            return mutation_result("replaced", document)
        except (DocumentNotFound, DocumentConflict, DocumentValidationError) as exc:
            logger.warning("Document operation failed: %s", exc)
            return f"Document operation failed: {exc}"

    @tool
    async def delete_document(uri: str, expected_revision: int) -> str:
        """Delete a model-created document in this conversation at its current revision.

        Uploaded documents and documents from other conversations cannot be deleted.
        """
        try:
            document_id = mutation_target(uri, expected_revision)
            await delete_generated_document(session_id, document_id, expected_revision)
            return json.dumps({"action": "deleted", "uri": f"document://{document_id}"})
        except (DocumentNotFound, DocumentConflict, DocumentValidationError) as exc:
            logger.warning("Document operation failed: %s", exc)
            return f"Document operation failed: {exc}"

    return [create_document, edit_document, delete_document, replace_document]
