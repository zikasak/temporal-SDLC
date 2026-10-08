"""API design sessions: one long-running Temporal workflow per discussion."""

import dataclasses
import logging
import uuid
from typing import Any, Literal

from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.api_design.models import ApiDesignState
from app.api_design.openapi import api_problems
from app.config import TASK_QUEUE
from app.documents import DocumentNotFound, get_generated_document
from app.requirements import service as requirements_service
from app.requirements.document import render_requirements_markdown
from app.temporal.client import connect_temporal
from app.temporal.sessions import SessionBusy, fork_messages, start_update, state_at_message

logger = logging.getLogger(__name__)


async def start_session(requirements_session_id: str) -> str:
    source = await requirements_service.get_state(requirements_session_id)
    if source.status != "completed" or not source.document:
        raise SessionBusy("Generate the requirements document before designing the API.")
    try:
        document = await get_generated_document(
            source.document["document_id"], requirements_session_id
        )
        requirements_document = document["parsed_text"]
    except DocumentNotFound:
        # The stored document expired; the requirements tables still carry the content.
        logger.info("Requirements document for %s expired; rendering from tables",
                    requirements_session_id)
        requirements_document = render_requirements_markdown(source.requirements)
    state = ApiDesignState(
        source_session_id=requirements_session_id,
        requirements=source.requirements,
        requirements_document=requirements_document,
        repository=source.repository,
    )
    return await _start(state)


async def _start(state: ApiDesignState) -> str:
    session_id = f"api-design-session-{uuid.uuid4().hex}"
    client = await connect_temporal()
    handle = await client.start_workflow(
        "ApiDesignWorkflow",
        state,
        id=session_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started API design session %s from %s", session_id, state.source_session_id)
    return handle.id


async def fork_session(session_id: str, message_index: int) -> str:
    """A new session with the discussion up to the message, and the API model
    as it was at that point."""
    source = await get_state(session_id)
    messages = fork_messages(source.messages, message_index)
    api = await state_at_message(
        session_id, source.messages, message_index,
        input_type=ApiDesignState,
        default_input=None,
        activity="api_design_turn",
        key="api",
    )
    fork_id = await _start(ApiDesignState(
        source_session_id=source.source_session_id,
        requirements=source.requirements,
        requirements_document=source.requirements_document,
        messages=messages,
        api=api,
        repository=source.repository,
        forked_from={"session_id": session_id, "message_index": message_index},
    ))
    logger.info("Forked API design session %s after message %d as %s",
                session_id, message_index, fork_id)
    return fork_id


async def send_message(session_id: str, message: str) -> None:
    await start_update(session_id, "send_message", message)
    logger.info("Sent message to API design session %s", session_id)


async def retry_message(session_id: str) -> None:
    await start_update(session_id, "retry_message")
    logger.info("Retrying message in API design session %s", session_id)


async def finalize_session(session_id: str) -> None:
    await start_update(session_id, "finalize")
    logger.info("Finalizing API design session %s", session_id)


async def retry_generation(session_id: str, stage: Literal['server', 'tests']) -> None:
    """Restart a failed stage, retaining the design and successful build artifacts."""
    if stage not in {'server', 'tests'}:
        raise SessionBusy('Choose server or tests generation to retry.')
    client = await connect_temporal()
    handle = client.get_workflow_handle(session_id)
    description = await handle.describe()
    if description.status != WorkflowExecutionStatus.COMPLETED:
        raise SessionBusy('Wait for the current generation to finish before retrying.')
    # Pin the query to the run we checked, rather than a concurrent restart.
    state = await client.get_workflow_handle(session_id, run_id=description.run_id).query(
        'get_state', result_type=ApiDesignState,
    )
    failed = (state.code_error or state.implementation_error) if stage == 'server' else state.tests_error
    if state.status != 'completed' or not state.document or not failed:
        raise SessionBusy('Only failed generation can be retried.')
    if stage == 'tests' and not state.code:
        raise SessionBusy('Retry server generation first.')
    state.generation_retry_stage = stage
    state.previous_run_id = description.run_id
    state.verification = None
    state.verification_error = None
    state.verification_status = 'waiting'
    state.repairs = []
    if stage == 'server':
        state.code_error = None
        state.implementation = None
        state.implementation_error = None
    else:
        state.tests = None
        state.tests_error = None
    try:
        await client.start_workflow(
            'ApiDesignWorkflow', state, id=session_id, task_queue=description.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
    except WorkflowAlreadyStartedError as exc:
        raise SessionBusy('Generation is already running for this session.') from exc
    logger.info('Restarted %s generation in API design session %s', stage, session_id)


async def get_state(session_id: str) -> ApiDesignState:
    client = await connect_temporal()
    handle = client.get_workflow_handle(session_id)
    return await handle.query("get_state", result_type=ApiDesignState)


async def get_session(session_id: str) -> dict[str, Any]:
    state = await get_state(session_id)
    session = dataclasses.asdict(state)
    # Polled every few seconds; the requirement list is enough for the page.
    del session["requirements_document"]
    del session['generation_retry_stage']
    del session['previous_run_id']
    return {"session_id": session_id, **session, "problems": api_problems(state.api)}
