import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.api.downloads import session_document_download
from app.api.errors import http_error
from app.api.routes.github import RepositoryInput
from app.github.service import resolve_repository_selection
from app.llm.questions import ANSWER_CHARS, MAX_QUESTIONS
from app.requirements.service import (
    answer_delivery_questions,
    answer_questions,
    finalize_session,
    fork_session,
    get_session,
    retry_generation,
    retry_message,
    send_message,
    start_session,
)

router = APIRouter(prefix="/requirements/sessions")
SESSION_ID_PATTERN = re.compile(r"requirements-session-[0-9a-f]{32}")


class CreateBody(BaseModel):
    # Every session works in a repository; derived sessions inherit this one.
    repository: RepositoryInput


class MessageBody(BaseModel):
    message: str = Field(max_length=10000)

    @field_validator("message")
    @classmethod
    def require_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be empty")
        return value


class AnswersBody(BaseModel):
    # One per question, in order: a chosen option or the user's own answer.
    answers: list[str] = Field(min_length=1, max_length=MAX_QUESTIONS)

    @field_validator("answers")
    @classmethod
    def require_answers(cls, value: list[str]) -> list[str]:
        value = [item.strip() for item in value]
        if not all(value) or any(len(item) > ANSWER_CHARS for item in value):
            raise ValueError(f"Each answer must contain 1 to {ANSWER_CHARS} characters")
        return value


class AgentAnswersBody(AnswersBody):
    # The stage workflow of the agent that asked.
    workflow_id: str = Field(max_length=300)


class RetryGenerationBody(BaseModel):
    stage: Literal['server', 'tests', 'documentation', 'repository']


class ForkBody(BaseModel):
    # The last message the fork keeps.
    message_index: int = Field(ge=0)


def _check_session_id(session_id: str) -> None:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise HTTPException(status_code=404, detail="Session not found")


@router.post("", status_code=201)
async def create_session(body: CreateBody):
    try:
        repository = await resolve_repository_selection(
            body.repository.full_name, body.repository.branch,
        )
        session_id = await start_session(repository=repository)
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
async def download_requirements(session_id: str):
    _check_session_id(session_id)
    try:
        session = await get_session(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return await session_document_download(session_id, session)
