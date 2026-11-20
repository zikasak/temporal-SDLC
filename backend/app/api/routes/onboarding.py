import re

from fastapi import APIRouter, Depends, HTTPException
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.api.errors import http_error
from app.api.routes.auth import require_user
from app.api.routes.github import RepositoryInput
from app.api.routes.requirements import (
    AgentAnswersBody, AnswersBody, FindingsInstructionsBody, ForkBody, MessageBody,
    RetryGenerationBody,
)
from app.github.service import resolve_repository_selection
from app.onboarding import service

router = APIRouter(prefix="/onboarding/sessions")
SESSION_ID_PATTERN = re.compile(r"onboarding-session-[0-9a-f]{32}")


class CreateBody(BaseModel):
    repository: RepositoryInput


class ContractDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # replace replaces everything existing_artifacts lists, decline ends the onboarding,
    # architecture_only adds only contracts/architecture.md and architecture.json.
    decision: Literal["replace", "decline", "architecture_only"]


def _check_session_id(session_id: str) -> None:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise HTTPException(status_code=404, detail="Session not found")


@router.post("", status_code=201)
async def create_session(body: CreateBody):
    try:
        repository = await resolve_repository_selection(
            body.repository.full_name, body.repository.branch,
        )
        # A new top-level task flow, whose onboarding interview this onboarding is.
        return await service.create_onboarding(repository)
    except Exception as exc:
        raise http_error(exc) from exc


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
        state = await service.get_state(session_id)
        await service.retry_message(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id,
            "status": "turn_running" if state.drafted else "drafting"}


@router.post("/{session_id}/checkout", status_code=202)
async def refresh_checkout(session_id: str):
    """Check the onboarding's branch out again at its latest commit."""
    _check_session_id(session_id)
    try:
        await service.update_checkout(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "preparing"}


@router.post("/{session_id}/contract-decision", status_code=202)
async def decide_contract(session_id: str, body: ContractDecisionBody):
    _check_session_id(session_id)
    try:
        status = await service.decide_contract(session_id, body.decision)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": status}


@router.post("/{session_id}/finalize", status_code=202)
async def finalize(session_id: str, force: bool = False):
    """Finalize the onboarding; force=true continues despite its requirements review."""
    _check_session_id(session_id)
    try:
        await service.finalize(session_id, force)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "finalizing"}


@router.post("/{session_id}/review/retry", status_code=202)
async def retry_review(session_id: str):
    """Run the failed requirements review round again with its original input."""
    _check_session_id(session_id)
    try:
        await service.retry_review(session_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "status": "finalizing"}


@router.post("/{session_id}/fork", status_code=201)
async def fork(session_id: str, body: ForkBody):
    _check_session_id(session_id)
    try:
        # The fork starts a new top-level task flow of its own.
        return await service.fork(session_id, body.message_index)
    except Exception as exc:
        raise http_error(exc) from exc


@router.post("/{session_id}/delivery/answers", status_code=202)
async def submit_agent_answers(session_id: str, body: AgentAnswersBody):
    _check_session_id(session_id)
    try:
        await service.answer_delivery_questions(session_id, body.workflow_id, body.answers)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id}


@router.post("/{session_id}/generation/retry", status_code=202)
async def restart_generation(session_id: str, body: RetryGenerationBody):
    _check_session_id(session_id)
    try:
        progress = await service.retry_generation(session_id, body.stage)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "verification_status": progress.verification_status}


@router.post("/{session_id}/delivery/findings/accept")
async def accept_open_findings(session_id: str, user: dict = Depends(require_user)):
    """Keep the onboarding pushed with open review findings as it is."""
    _check_session_id(session_id)
    try:
        progress = await service.accept_findings(session_id, user["login"])
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "verification_status": progress.verification_status}


@router.post("/{session_id}/delivery/findings/instructions", status_code=202)
async def instruct_open_findings(session_id: str, body: FindingsInstructionsBody):
    """Start a fix round from the user's instructions about open review findings."""
    _check_session_id(session_id)
    try:
        progress = await service.instruct_findings(session_id, body.instructions)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"session_id": session_id, "verification_status": progress.verification_status}
