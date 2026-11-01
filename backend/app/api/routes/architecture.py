import re

from fastapi import APIRouter, HTTPException

from app.api.downloads import session_document_download
from app.api.errors import http_error
from app.api.routes.requirements import AnswersBody, ForkBody, MessageBody
from app.architecture import service

router = APIRouter(prefix="/architecture/sessions")
SESSION_ID_PATTERN = re.compile(r"architecture-session-[0-9a-f]{32}")
FORMATS = ("md", "json")


def _check_session_id(session_id: str) -> None:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise HTTPException(status_code=404, detail="Session not found")


@router.get("/{session_id}")
async def retrieve_session(session_id: str):
    _check_session_id(session_id)
    try:
        return await service.get_session(session_id)
    except Exception as exc:
        raise http_error(exc) from exc


@router.post("/{session_id}/messages", status_code=202)
async def submit_message(session_id: str, body: MessageBody):
    _check_session_id(session_id)
    try:
        await service.send_message(session_id, body.message)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "turn_running"}


@router.post("/{session_id}/answers", status_code=202)
async def submit_answers(session_id: str, body: AnswersBody):
    _check_session_id(session_id)
    try:
        await service.answer_questions(session_id, body.answers)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "turn_running"}


@router.post("/{session_id}/retry", status_code=202)
async def retry(session_id: str):
    """Run the failed drafting or interviewer turn again."""
    _check_session_id(session_id)
    try:
        status = await service.retry_message(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": status}


@router.post("/{session_id}/finalize", status_code=202)
async def finalize(session_id: str):
    _check_session_id(session_id)
    try:
        await service.finalize_session(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "finalizing"}


@router.post("/{session_id}/skip", status_code=202)
async def skip(session_id: str):
    """Skip the architecture stage of an API change; the API design starts."""
    _check_session_id(session_id)
    try:
        await service.skip_stage(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "skipped"}


@router.post("/{session_id}/fork", status_code=201)
async def fork(session_id: str, body: ForkBody):
    _check_session_id(session_id)
    try:
        fork_id = await service.fork_session(session_id, body.message_index)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": fork_id}


@router.get("/{session_id}/document")
async def download_architecture(session_id: str, format: str = "md"):
    """architecture.md, or with format json architecture.json."""
    _check_session_id(session_id)
    if format not in FORMATS:
        raise HTTPException(status_code=422, detail="format must be md or json.")
    try:
        session = await service.get_session(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    document = session["document"]
    if document and format == "json":
        document = document.get("json")
    return await session_document_download(session_id, {"document": document})
