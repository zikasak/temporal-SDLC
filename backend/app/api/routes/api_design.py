import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.downloads import session_document_download
from app.api.errors import http_error
from app.api.routes.requirements import ForkBody, MessageBody
from app.api_design.service import (
    finalize_session,
    fork_session,
    get_session,
    retry_generation,
    retry_message,
    send_message,
    start_session,
)

router = APIRouter(prefix="/api-design/sessions")
SESSION_ID_PATTERN = re.compile(r"api-design-session-[0-9a-f]{32}")


class StartBody(BaseModel):
    requirements_session_id: str = Field(pattern=r"^requirements-session-[0-9a-f]{32}$")


class RetryGenerationBody(BaseModel):
    stage: Literal['server', 'tests', 'repository']


def _check_session_id(session_id: str) -> None:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise HTTPException(status_code=404, detail="Session not found")


@router.post("", status_code=201)
async def create_session(body: StartBody):
    try:
        session_id = await start_session(body.requirements_session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id}


@router.get("/{session_id}")
async def retrieve_session(session_id: str):
    _check_session_id(session_id)
    try:
        return await get_session(session_id)
    except Exception as exc:
        raise http_error(exc) from exc


@router.post("/{session_id}/messages", status_code=202)
async def submit_message(session_id: str, body: MessageBody):
    _check_session_id(session_id)
    try:
        await send_message(session_id, body.message)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "turn_running"}


@router.post("/{session_id}/retry", status_code=202)
async def retry(session_id: str):
    _check_session_id(session_id)
    try:
        await retry_message(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "turn_running"}


@router.post('/{session_id}/generation/retry', status_code=202)
async def restart_generation(session_id: str, body: RetryGenerationBody):
    _check_session_id(session_id)
    try:
        await retry_generation(session_id, body.stage)
    except Exception as exc:
        raise http_error(exc) from exc
    return {'session_id': session_id, 'verification_status': 'waiting'}


@router.post("/{session_id}/finalize", status_code=202)
async def finalize(session_id: str):
    _check_session_id(session_id)
    try:
        await finalize_session(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "finalizing"}


@router.post("/{session_id}/fork", status_code=201)
async def fork(session_id: str, body: ForkBody):
    _check_session_id(session_id)
    try:
        fork_id = await fork_session(session_id, body.message_index)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": fork_id}


@router.get("/{session_id}/document")
async def download_openapi(session_id: str):
    _check_session_id(session_id)
    try:
        session = await get_session(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return await session_document_download(session_id, session)
