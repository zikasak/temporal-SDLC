"""API design sessions: one long-running Temporal workflow per discussion."""

import dataclasses
import logging
import uuid
from typing import Any

from temporalio.client import WorkflowUpdateStage

from app.api_design.models import ApiDesignState
from app.api_design.openapi import design_problems
from app.config import TASK_QUEUE
from app.requirements.lookup import source_session_lookup
from app.delivery import service as delivery_service
from app.delivery.models import DeliveryProgress, RetryStage
from app.tasks.service import start_fork
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import execution_fields
from app.temporal.registry import register
from app.temporal.sessions import (
    FLOW_CLOSED, SessionBusy, SessionNotFound, flow_closed, fork_messages, is_running,
    start_update, state_at_message,
)

logger = logging.getLogger(__name__)


def new_session_id() -> str:
    return f"api-design-session-{uuid.uuid4().hex}"


async def _start(state: ApiDesignState) -> str:
    """Start the session as a child of the requirements session its task began with, so
    the task stays one workflow tree; a requirements session closed before that existed
    can only have it on its own. Only for designs from before task flows existed: a task
    flow starts its own."""
    session_id = new_session_id()
    if await is_running(state.source_session_id):
        await start_update(state.source_session_id, "start_api_design", session_id, state,
                           wait_for_stage=WorkflowUpdateStage.COMPLETED)
    else:
        client = await connect_temporal()
        await client.start_workflow(
            "ApiDesignWorkflow",
            state,
            id=session_id,
            task_queue=TASK_QUEUE,
            **execution_fields(state.repository),
        )
    await register(session_id)
    logger.info("Started API design session %s from %s", session_id, state.source_session_id)
    return session_id


async def fork_session(session_id: str, message_index: int) -> str:
    """A new session with the discussion up to the message, and the API model
    as it was at that point; 409 once its flow has completed or been cancelled."""
    fork_id, _ = await fork_task(session_id, message_index)
    return fork_id


async def fork_task(session_id: str, message_index: int) -> tuple[str, str | None]:
    """fork_session, also returning the new task flow's ID; None for a design started
    before task flows existed, whose fork is a child of its requirements session."""
    source = await get_state(session_id)
    flow = source.task_flow_id or source.source_session_id
    if flow and await flow_closed(flow):
        raise SessionBusy(FLOW_CLOSED)
    messages = fork_messages(source.messages, message_index)
    api = await state_at_message(
        session_id, source.messages, message_index,
        input_type=ApiDesignState,
        default_input=None,
        activity="api_design_turn",
        key="api",
    )
    forked = ApiDesignState(
        source_session_id=source.source_session_id,
        requirements=source.requirements,
        requirements_document=source.requirements_document,
        change_document=source.change_document,
        contract=source.contract,
        contract_commit=source.contract_commit,
        kind=source.kind,
        messages=messages,
        api=api,
        # The choice is kept even when the fork point precedes it; the user can change it.
        language=source.language,
        repository=source.repository,
        forked_from={"session_id": session_id, "message_index": message_index},
        # A fork designs against the same architecture.
        architecture_session_id=source.architecture_session_id,
        architecture=source.architecture,
        architecture_structure=source.architecture_structure,
    )
    if not source.task_flow_id:
        fork_id = await _start(forked)
        logger.info("Forked API design session %s after message %d as %s",
                    session_id, message_index, fork_id)
        return fork_id, None
    # The fork starts a task flow of its own, linking to the original's requirements and
    # architecture, with its own pending delivery.
    fork_id = new_session_id()
    task_flow_id = await start_fork(source.task_flow_id, "api_design", fork_id, forked,
                                    kind=source.kind, repository=source.repository,
                                    client=await connect_temporal(),
                                    source_session_id=session_id)
    await register(fork_id)
    logger.info("Forked API design session %s after message %d as %s in task flow %s",
                session_id, message_index, fork_id, task_flow_id)
    return fork_id, task_flow_id


async def send_message(session_id: str, message: str) -> None:
    await start_update(session_id, "send_message", message)
    logger.info("Sent message to API design session %s", session_id)


async def answer_questions(session_id: str, answers: list[str]) -> None:
    await start_update(session_id, "answer_questions", answers)
    logger.info("Sent answers to API design session %s", session_id)


async def answer_delivery_questions(session_id: str, workflow_id: str,
                                    answers: list[str]) -> None:
    await delivery_service.answer(session_id, workflow_id, answers)


async def retry_message(session_id: str) -> None:
    await start_update(session_id, "retry_message")
    logger.info("Retrying message in API design session %s", session_id)


async def finalize_session(session_id: str) -> None:
    await start_update(session_id, "finalize")
    logger.info("Finalizing API design session %s", session_id)


async def retry_generation(session_id: str, stage: RetryStage) -> DeliveryProgress:
    state = await get_state(session_id)
    if state.status != 'completed' or not state.document:
        raise SessionBusy('Only failed generation can be retried.')
    return await delivery_service.retry(session_id, stage)


async def _check_delivered(session_id: str) -> None:
    state = await get_state(session_id)
    if state.status != 'completed' or not state.document:
        raise SessionBusy('The delivery has no open review findings.')


async def accept_findings(session_id: str, login: str) -> DeliveryProgress:
    await _check_delivered(session_id)
    return await delivery_service.accept_findings(session_id, login)


async def instruct_findings(session_id: str, instructions: str) -> DeliveryProgress:
    await _check_delivered(session_id)
    return await delivery_service.instruct_findings(session_id, instructions)


async def get_state(session_id: str) -> ApiDesignState:
    """The session's state; SessionNotFound for a stage of a task flow that has not begun,
    which is not a session yet."""
    client = await connect_temporal()
    handle = client.get_workflow_handle(session_id)
    state = await handle.query("get_state", result_type=ApiDesignState)
    if state.pending:
        raise SessionNotFound("Session not found: the API design stage has not started.")
    return state


async def get_session(session_id: str) -> dict[str, Any]:
    state = await get_state(session_id)
    session = dataclasses.asdict(state)
    # Polled every few seconds; the requirement list is enough for the page.
    del session["requirements_document"], session["change_document"], session["contract"]
    del session["pending"]
    # The pipeline's results belong to the delivery a finished design starts.
    progress = DeliveryProgress()
    if state.status == 'completed' and state.document:
        progress = (await delivery_service.get_progress(session_id)
                    or DeliveryProgress(verification_status='waiting'))
    lookup = await source_session_lookup(
        state.source_session_id, state.requirements,
        (state.contract or {}).get("requirements"))
    return {"session_id": session_id, **session, **dataclasses.asdict(progress),
            "problems": design_problems(state.api, state.language),
            "lookup_requirements": lookup}
