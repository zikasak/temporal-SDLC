from typing import Any
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import Response

from app.api.errors import http_error
from app.documents import DocumentNotFound, get_generated_document


async def session_document_download(session_id: str, session: dict[str, Any]) -> Response:
    """The document a session workflow produced, as a file download."""
    if not session["document"]:
        raise HTTPException(status_code=404, detail="The document is not ready yet")
    try:
        document = await get_generated_document(session["document"]["document_id"], session_id)
    except DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc
    except Exception as exc:
        raise http_error(exc) from exc
    return Response(
        content=document["parsed_text"].encode("utf-8"),
        media_type=document["content_type"],
        headers={"Content-Disposition":
                 f"attachment; filename*=utf-8''{quote(document['filename'])}"},
    )
