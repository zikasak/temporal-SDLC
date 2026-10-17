# coding: utf-8

from typing import Dict, List  # noqa: F401
import importlib
import pkgutil

from service.apis.requirements_api_base import BaseRequirementsApi
import service.impl

from fastapi import (  # noqa: F401
    APIRouter,
    Body,
    Cookie,
    Depends,
    Form,
    Header,
    HTTPException,
    Path,
    Query,
    Response,
    Security,
    status,
)

from service.models.extra_models import TokenModel  # noqa: F401
from pydantic import Field, StrictStr
from typing import Any
from typing_extensions import Annotated
from service.models.agent_answers_input import AgentAnswersInput
from service.models.answers_input import AnswersInput
from service.models.create_requirements_session import CreateRequirementsSession
from service.models.error import Error
from service.models.findings_instructions_input import FindingsInstructionsInput
from service.models.fork_input import ForkInput
from service.models.intake import Intake
from service.models.message_input import MessageInput
from service.models.requirements_session import RequirementsSession
from service.models.retry_stage_input import RetryStageInput
from service.models.session_created import SessionCreated
from service.models.session_status_change import SessionStatusChange
from service.security_api import get_token_bearer

router = APIRouter()

ns_pkg = service.impl
for _, name, _ in pkgutil.iter_modules(ns_pkg.__path__, ns_pkg.__name__ + "."):
    importlib.import_module(name)


@router.get(
    "/requirements/intake",
    responses={
        200: {"model": Intake, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
    },
    tags=["requirements"],
    summary="Requirements intake",
    response_model_by_alias=True,
)
async def get_requirements_intake(
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> Intake:
    """The questions a requirements session starts with."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().get_requirements_intake()


@router.post(
    "/requirements/sessions",
    responses={
        201: {"model": SessionCreated, "description": "Created"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["requirements"],
    summary="Start a requirements session",
    response_model_by_alias=True,
)
async def post_requirements_sessions(
    create_requirements_session: CreateRequirementsSession = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> SessionCreated:
    """Starts the interview in the repository from the intake&#39;s answers."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().post_requirements_sessions(create_requirements_session)


@router.get(
    "/requirements/sessions/{session_id}",
    responses={
        200: {"model": RequirementsSession, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
    },
    tags=["requirements"],
    summary="Get a requirements session",
    response_model_by_alias=True,
)
async def get_requirements_sessions_by_session_id(
    session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")] = Path(..., description="requirements-session- followed by 32 hex digits."),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> RequirementsSession:
    """The session&#39;s conversation, state and delivery progress."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().get_requirements_sessions_by_session_id(session_id)


@router.post(
    "/requirements/sessions/{session_id}/answers",
    responses={
        202: {"model": SessionStatusChange, "description": "Accepted"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["requirements"],
    summary="Answer the interviewer&#39;s questions",
    response_model_by_alias=True,
)
async def post_requirements_sessions_by_session_id_answers(
    session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")] = Path(..., description="requirements-session- followed by 32 hex digits."),
    answers_input: AnswersInput = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> SessionStatusChange:
    """Answers the questions of the last assistant message and starts a turn."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().post_requirements_sessions_by_session_id_answers(session_id, answers_input)


@router.post(
    "/requirements/sessions/{session_id}/checkout",
    responses={
        202: {"model": SessionStatusChange, "description": "Accepted"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
    },
    tags=["requirements"],
    summary="Update the session&#39;s checkout",
    response_model_by_alias=True,
)
async def post_requirements_sessions_by_session_id_checkout(
    session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")] = Path(..., description="requirements-session- followed by 32 hex digits."),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> SessionStatusChange:
    """Checks the session&#39;s branch out again at its latest commit; the session is preparing until it is. Refused while a message is unanswered or once the session is finalized."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().post_requirements_sessions_by_session_id_checkout(session_id)


@router.post(
    "/requirements/sessions/{session_id}/delivery/answers",
    responses={
        202: {"model": SessionCreated, "description": "Accepted"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["requirements"],
    summary="Answer a delivery agent&#39;s questions",
    response_model_by_alias=True,
)
async def post_requirements_sessions_by_session_id_delivery_answers(
    session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")] = Path(..., description="requirements-session- followed by 32 hex digits."),
    agent_answers_input: AgentAnswersInput = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> SessionCreated:
    """Resumes the paused coding agent of the named stage workflow."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().post_requirements_sessions_by_session_id_delivery_answers(session_id, agent_answers_input)


@router.post(
    "/requirements/sessions/{session_id}/delivery/findings/accept",
    responses={
        200: {"model": SessionStatusChange, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
        503: {"model": Error, "description": "Service Unavailable"},
    },
    tags=["requirements"],
    summary="Accept the delivery with open review findings",
    response_model_by_alias=True,
)
async def post_requirements_sessions_by_session_id_delivery_findings_accept(
    session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")] = Path(..., description="requirements-session- followed by 32 hex digits."),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> SessionStatusChange:
    """Clears the open-findings flag, keeps the pushed delivery as it is and records findings_acceptance (when and by whom) in the delivery progress. The publication stays marked as unverified. 409 while no flag is raised."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().post_requirements_sessions_by_session_id_delivery_findings_accept(session_id)


@router.post(
    "/requirements/sessions/{session_id}/delivery/findings/instructions",
    responses={
        202: {"model": SessionStatusChange, "description": "Accepted"},
        401: {"model": Error, "description": "Unauthorized"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
        413: {"model": Error, "description": "Request Entity Too Large"},
        422: {"model": Error, "description": "Unprocessable Entity"},
        503: {"model": Error, "description": "Service Unavailable"},
    },
    tags=["requirements"],
    summary="Give instructions about open review findings",
    response_model_by_alias=True,
)
async def post_requirements_sessions_by_session_id_delivery_findings_instructions(
    session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")] = Path(..., description="requirements-session- followed by 32 hex digits."),
    findings_instructions_input: FindingsInstructionsInput = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> SessionStatusChange:
    """Starts a fix round from the instructions, then verification and code review, pushing the result to the same branch. The instructions are recorded on the review round they start. If verification fails for good, verification_status becomes error, the branch keeps the earlier unverified push and the flag stays raised. 409 while no flag is raised."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().post_requirements_sessions_by_session_id_delivery_findings_instructions(session_id, findings_instructions_input)


@router.get(
    "/requirements/sessions/{session_id}/document",
    responses={
        200: {"description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
    },
    tags=["requirements"],
    summary="Download the requirements document",
    response_model_by_alias=True,
)
async def get_requirements_sessions_by_session_id_document(
    session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")] = Path(..., description="requirements-session- followed by 32 hex digits."),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> None:
    """The requirements document as a file attachment; 404 until it is ready."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().get_requirements_sessions_by_session_id_document(session_id)


@router.post(
    "/requirements/sessions/{session_id}/finalize",
    responses={
        202: {"model": SessionStatusChange, "description": "Accepted"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
    },
    tags=["requirements"],
    summary="Finalize the requirements session",
    response_model_by_alias=True,
)
async def post_requirements_sessions_by_session_id_finalize(
    session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")] = Path(..., description="requirements-session- followed by 32 hex digits."),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> SessionStatusChange:
    """Writes the requirements document; the session is completed once it is ready."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().post_requirements_sessions_by_session_id_finalize(session_id)


@router.post(
    "/requirements/sessions/{session_id}/fork",
    responses={
        201: {"model": SessionCreated, "description": "Created"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["requirements"],
    summary="Fork the session",
    response_model_by_alias=True,
)
async def post_requirements_sessions_by_session_id_fork(
    session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")] = Path(..., description="requirements-session- followed by 32 hex digits."),
    fork_input: ForkInput = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> SessionCreated:
    """Starts a new session with the conversation up to the given message."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().post_requirements_sessions_by_session_id_fork(session_id, fork_input)


@router.post(
    "/requirements/sessions/{session_id}/generation/retry",
    responses={
        202: {"model": SessionStatusChange, "description": "Accepted"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["requirements"],
    summary="Retry a delivery stage",
    response_model_by_alias=True,
)
async def post_requirements_sessions_by_session_id_generation_retry(
    session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")] = Path(..., description="requirements-session- followed by 32 hex digits."),
    retry_stage_input: RetryStageInput = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> SessionStatusChange:
    """Starts the named stage of the delivery over. The review stage runs a failed code review again; 409 when the delivery has no failed review."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().post_requirements_sessions_by_session_id_generation_retry(session_id, retry_stage_input)


@router.post(
    "/requirements/sessions/{session_id}/messages",
    responses={
        202: {"model": SessionStatusChange, "description": "Accepted"},
        401: {"model": Error, "description": "Unauthorized"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
        413: {"model": Error, "description": "Request Entity Too Large"},
        422: {"model": Error, "description": "Unprocessable Entity"},
        503: {"model": Error, "description": "Service Unavailable"},
    },
    tags=["requirements"],
    summary="Send a message",
    response_model_by_alias=True,
)
async def post_requirements_sessions_by_session_id_messages(
    session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")] = Path(..., description="requirements-session- followed by 32 hex digits."),
    message_input: MessageInput = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> SessionStatusChange:
    """Starts the interviewer&#39;s turn on the message."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().post_requirements_sessions_by_session_id_messages(session_id, message_input)


@router.post(
    "/requirements/sessions/{session_id}/retry",
    responses={
        202: {"model": SessionStatusChange, "description": "Accepted"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
    },
    tags=["requirements"],
    summary="Retry the last turn",
    response_model_by_alias=True,
)
async def post_requirements_sessions_by_session_id_retry(
    session_id: Annotated[StrictStr, Field(description="requirements-session- followed by 32 hex digits.")] = Path(..., description="requirements-session- followed by 32 hex digits."),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> SessionStatusChange:
    """Runs the failed interviewer turn again."""
    if not BaseRequirementsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseRequirementsApi.subclasses[0]().post_requirements_sessions_by_session_id_retry(session_id)
