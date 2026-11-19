import re
from fastapi import APIRouter, Depends, HTTPException

from app.api.downloads import session_document_download
from app.api.errors import http_error
from app.api.routes.auth import require_user
from app.api.routes.requirements import (
    AgentAnswersBody, AnswersBody, FindingsInstructionsBody, ForkBody, MessageBody,
    RetryGenerationBody,
)
from app.api_design.service import (
    accept_findings,
    instruct_findings,
    answer_delivery_questions,
    answer_questions,
    finalize_session,
    fork as fork_task_session,
    get_session,
    retry_generation,
    retry_message,
    send_message,
)

router = APIRouter(prefix="/api-design/sessions")
SESSION_ID_PATTERN = re.compile(r"api-design-session-[0-9a-f]{32}")


def _check_session_id(session_id: str) -> None:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise HTTPException(status_code=404, detail="Session not found")


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


@router.post("/{session_id}/answers", status_code=202)
async def submit_answers(session_id: str, body: AnswersBody):
    _check_session_id(session_id)
    try:
        await answer_questions(session_id, body.answers)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "turn_running"}


@router.post("/{session_id}/delivery/answers", status_code=202)
async def submit_agent_answers(session_id: str, body: AgentAnswersBody):
    _check_session_id(session_id)
    try:
        await answer_delivery_questions(session_id, body.workflow_id, body.answers)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id}


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
        progress = await retry_generation(session_id, body.stage)
    except Exception as exc:
        raise http_error(exc) from exc
    return {'session_id': session_id, 'verification_status': progress.verification_status}


@router.post('/{session_id}/delivery/findings/accept')
async def accept_open_findings(session_id: str, user: dict = Depends(require_user)):
    """Keep the delivery pushed with open review findings as it is."""
    _check_session_id(session_id)
    try:
        progress = await accept_findings(session_id, user['login'])
    except Exception as exc:
        raise http_error(exc) from exc
    return {'session_id': session_id, 'verification_status': progress.verification_status}


@router.post('/{session_id}/delivery/findings/instructions', status_code=202)
async def instruct_open_findings(session_id: str, body: FindingsInstructionsBody):
    """Start a fix round from the user's instructions about open review findings."""
    _check_session_id(session_id)
    try:
        progress = await instruct_findings(session_id, body.instructions)
    except Exception as exc:
        raise http_error(exc) from exc
    return {'session_id': session_id, 'verification_status': progress.verification_status}


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
        # The fork starts a new top-level task flow of its own.
        return await fork_task_session(session_id, body.message_index)
    except Exception as exc:
        raise http_error(exc) from exc


@router.get("/{session_id}/document")
async def download_openapi(session_id: str):
    _check_session_id(session_id)
    try:
        session = await get_session(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return await session_document_download(session_id, session)
