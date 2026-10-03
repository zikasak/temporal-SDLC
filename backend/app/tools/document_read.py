"""Tools that read uploaded and model-created documents."""

import json

from langchain_core.tools import BaseTool, tool

from app.documents import DocumentNotFound, read_document_text, search_document_text
from app.tools.context import MAX_TOOL_CHARS, ToolContext


def build(ctx: ToolContext) -> list[BaseTool]:
    @tool
    async def read_document(uri: str, offset: int = 0, max_chars: int = MAX_TOOL_CHARS) -> str:
        """Read parsed text from an uploaded or model-created document:// URI."""
        authorized = await ctx.authorized_document(uri)
        if authorized is None:
            return "Unknown or unavailable document link in this conversation."
        document_id, revision = authorized
        if offset < 0:
            return "Offset must be zero or greater."
        try:
            content, total = await read_document_text(
                document_id, offset, max(1, min(max_chars, MAX_TOOL_CHARS))
            )
        except DocumentNotFound as exc:
            return str(exc)
        await ctx.remember(document_id, revision, offset, content)
        return json.dumps(
            {"uri": uri, "offset": offset, "text": content,
             "next_offset": offset + len(content), "total_characters": total,
             "revision": revision},
            ensure_ascii=False,
        )

    @tool
    async def search_document(uri: str, query: str) -> str:
        """Find up to five passages in an uploaded or model-created document.

        Matches each keyword separately (case-insensitive) and ranks passages by
        how many keywords they contain. Use short distinctive keywords.
        """
        authorized = await ctx.authorized_document(uri)
        if authorized is None:
            return "Unknown or unavailable document link in this conversation."
        document_id, revision = authorized
        if not query or len(query) > 200:
            return "Search query must contain 1 to 200 characters."
        try:
            matches, total = await search_document_text(document_id, query)
        except DocumentNotFound as exc:
            return str(exc)
        result: dict[str, object] = {
            "uri": uri, "revision": revision, "total_characters": total,
            "matches": matches,
        }
        if not matches:
            result["hint"] = (
                "No matches. Retry with single keywords or synonyms, or call "
                "read_document from offset 0 and follow next_offset."
            )
        for match in matches:
            await ctx.remember(document_id, revision, match["offset"], match["snippet"])
        return json.dumps(result, ensure_ascii=False)

    return [read_document, search_document]
