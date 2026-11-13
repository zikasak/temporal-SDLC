import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.downloads import session_document_download
from app.api.errors import http_error
from app.api.routes.auth import require_user
from app.api.routes.github import RepositoryInput
from app.delivery import service as delivery_service
from app.github.service import resolve_repository_selection
from app.llm.questions import ANSWER_CHARS, MAX_QUESTIONS
from app.requirements.models import TaskKind
from app.requirements.prompt import INTAKE_QUESTIONS, intake
from app.review.models import INSTRUCTIONS_CHARS
from app.requirements.service import (
    accept_findings,
    instruct_findings,
    answer_delivery_questions,
    answer_questions,
    finalize_session,
    fork_task,
    get_session,
    retry_generation,
    retry_message,
    send_message,
    start_task,
    update_checkout,
)

router = APIRouter(prefix="/requirements")
SESSION_ID_PATTERN = re.compile(r"requirements-session-[0-9a-f]{32}")


def _clean_answers(value: list[str]) -> list[str]:
    value = [item.strip() for item in value]
    if not all(value) or any(len(item) > ANSWER_CHARS for item in value):
        raise ValueError(f"Each answer must contain 1 to {ANSWER_CHARS} characters")
    return value


class CreateBody(BaseModel):
    # Every session works in a repository; derived sessions inherit this one.
    repository: RepositoryInput
    # The intake: the kind the user chose, their answers to its questions in order (a
    # chosen option or their own), and what needs to be done.
    kind: TaskKind
    answers: list[str] = Field(max_length=MAX_QUESTIONS)
    task: str = Field(max_length=10000)

    @field_validator("answers")
    @classmethod
    def require_answers(cls, value: list[str]) -> list[str]:
        return _clean_answers(value)

    @field_validator("task")
    @classmethod
    def require_task(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Say what needs to be done")
        return value

    @model_validator(mode="after")
    def answer_every_question(self) -> "CreateBody":
        if len(self.answers) != len(INTAKE_QUESTIONS[self.kind]):
            raise ValueError(f"Answer all {len(INTAKE_QUESTIONS[self.kind])} questions "
                             "of this kind of task")
        return self


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
        return _clean_answers(value)


class AgentAnswersBody(AnswersBody):
    # The stage workflow of the agent that asked.
    workflow_id: str = Field(max_length=300)


class RetryGenerationBody(BaseModel):
    # review runs a failed code review again on the verified delivery, or goes on from a
    # correction of review findings that did not pass verification.
    stage: Literal['server', 'tests', 'documentation', 'repository', 'review']


class FindingsInstructionsBody(BaseModel):
    instructions: str

    @field_validator("instructions")
    @classmethod
    def require_instructions(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Instructions cannot be empty")
        if len(value) > INSTRUCTIONS_CHARS:
            raise ValueError(f"Instructions have at most {INSTRUCTIONS_CHARS} characters")
        return value


class ForkBody(BaseModel):
    # The last message the fork keeps.
    message_index: int = Field(ge=0)


def _check_session_id(session_id: str) -> None:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise HTTPException(status_code=404, detail="Session not found")


# A flow that carries on a closed delivery on a new branch, as a top-level flow of its own:
# it has no interview, and its session reports only its delivery, which the delivery
# endpoints below act on as on a requirements session's own delivery.
FOLLOW_UP_ID_PATTERN = re.compile(r"fix-session-[0-9a-f]{32}")


def _follow_up(session_id: str) -> bool:
    if FOLLOW_UP_ID_PATTERN.fullmatch(session_id):
        return True
    _check_session_id(session_id)
    return False


@router.get("/intake")
async def retrieve_intake():
    return intake()


@router.post("/sessions", status_code=201)
async def create_session(body: CreateBody):
    try:
        repository = await resolve_repository_selection(
            body.repository.full_name, body.repository.branch,
        )
        session_id, task_flow_id = await start_task(repository=repository, kind=body.kind,
                                                    answers=body.answers, task=body.task)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "task_flow_id": task_flow_id}


@router.get("/sessions/{session_id}")
async def retrieve_session(session_id: str):
    follow_up = _follow_up(session_id)
    try:
        if follow_up:
            return await delivery_service.follow_up_session(session_id)
        return await get_session(session_id)
    except Exception as exc:
        raise http_error(exc) from exc


@router.post("/sessions/{session_id}/messages", status_code=202)
async def submit_message(session_id: str, body: MessageBody):
    _check_session_id(session_id)
    try:
        await send_message(session_id, body.message)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "turn_running"}


@router.post("/sessions/{session_id}/answers", status_code=202)
async def submit_answers(session_id: str, body: AnswersBody):
    _check_session_id(session_id)
    try:
        await answer_questions(session_id, body.answers)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "turn_running"}


@router.post("/sessions/{session_id}/delivery/answers", status_code=202)
async def submit_agent_answers(session_id: str, body: AgentAnswersBody):
    follow_up = _follow_up(session_id)
    try:
        if follow_up:
            await delivery_service.answer(session_id, body.workflow_id, body.answers)
        else:
            await answer_delivery_questions(session_id, body.workflow_id, body.answers)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id}


@router.post("/sessions/{session_id}/retry", status_code=202)
async def retry(session_id: str):
    _check_session_id(session_id)
    try:
        await retry_message(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "turn_running"}


@router.post('/sessions/{session_id}/generation/retry', status_code=202)
async def restart_generation(session_id: str, body: RetryGenerationBody):
    follow_up = _follow_up(session_id)
    try:
        progress = await (delivery_service.retry(session_id, body.stage) if follow_up
                          else retry_generation(session_id, body.stage))
    except Exception as exc:
        raise http_error(exc) from exc
    return {'session_id': session_id, 'verification_status': progress.verification_status}


@router.post('/sessions/{session_id}/delivery/findings/accept')
async def accept_open_findings(session_id: str, user: dict = Depends(require_user)):
    """Keep the delivery pushed with open review findings as it is."""
    follow_up = _follow_up(session_id)
    try:
        progress = await (delivery_service.accept_findings(session_id, user['login'])
                          if follow_up else accept_findings(session_id, user['login']))
    except Exception as exc:
        raise http_error(exc) from exc
    return {'session_id': session_id, 'verification_status': progress.verification_status}


@router.post('/sessions/{session_id}/delivery/findings/instructions', status_code=202)
async def instruct_open_findings(session_id: str, body: FindingsInstructionsBody):
    """Start a fix round from the user's instructions about open review findings."""
    follow_up = _follow_up(session_id)
    try:
        progress = await (delivery_service.instruct_findings(session_id, body.instructions)
                          if follow_up else instruct_findings(session_id, body.instructions))
    except Exception as exc:
        raise http_error(exc) from exc
    return {'session_id': session_id, 'verification_status': progress.verification_status}


@router.post("/sessions/{session_id}/finalize", status_code=202)
async def finalize(session_id: str):
    _check_session_id(session_id)
    try:
        await finalize_session(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "finalizing"}


@router.post("/sessions/{session_id}/checkout", status_code=202)
async def refresh_checkout(session_id: str):
    """Check the session's branch out again at its latest commit."""
    _check_session_id(session_id)
    try:
        await update_checkout(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "preparing"}


@router.post("/sessions/{session_id}/fork", status_code=201)
async def fork(session_id: str, body: ForkBody):
    _check_session_id(session_id)
    try:
        fork_id, task_flow_id = await fork_task(session_id, body.message_index)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": fork_id, "task_flow_id": task_flow_id}


@router.get("/sessions/{session_id}/document")
async def download_requirements(session_id: str):
    _check_session_id(session_id)
    try:
        session = await get_session(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return await session_document_download(session_id, session)
