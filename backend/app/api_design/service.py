"""API design sessions: one long-running Temporal workflow per discussion, started by its
task flow once the architecture or the requirements hand the task on, or as a fork in a
task flow of its own."""

import dataclasses
import logging
from typing import Any

from app.api_design.models import ApiDesignState
from app.api_design.openapi import design_problems
from app.requirements.document import render_requirements_markdown
from app.requirements.lookup import source_session_lookup
from app.delivery import service as delivery_service
from app.delivery.models import DeliveryProgress, RetryStage
from app.task_flow.models import FORK_SUFFIX, TaskFlowState
from app.task_flow.service import check_open, derived_name, new_stage_id, start_task_flow
from app.temporal.client import connect_temporal
from app.temporal.sessions import (
    SessionBusy, SessionNotFound, fork_messages, start_update, state_at_message,
)

logger = logging.getLogger(__name__)


async def _start(state: ApiDesignState, source: ApiDesignState) -> tuple[str, str]:
    """Start the session in a new task flow of its own, named after the source's with a
    " (fork)" suffix, which links to the task's requirements and architecture sessions and
    holds a pending delivery."""
    session_id = new_stage_id("api_design")
    name, suffix = await derived_name(source.task_flow_id, FORK_SUFFIX)
    linked = [item for item in (state.source_session_id, state.architecture_session_id)
              if item]
    flow_id = await start_task_flow(TaskFlowState(
        kind=state.kind, repository=state.repository,
        task=render_requirements_markdown(state.requirements),
        name=name, name_suffix=suffix, origin="fork",
        first_stage="api_design", first_stage_id=session_id, design=state, linked=linked))
    logger.info("Started API design session %s from %s in task flow %s", session_id,
                state.source_session_id, flow_id)
    return session_id, flow_id


async def fork_session(session_id: str, message_index: int) -> str:
    """As fork; returns the fork's session ID."""
    fork_id, _ = await fork(session_id, message_index)
    return fork_id


async def fork(session_id: str, message_index: int) -> tuple[str, str]:
    """A new session with the discussion up to the message, and the API model as it was
    at that point, in a new task flow; 409 once its flow has completed or been cancelled.
    Returns the fork's and its task flow's IDs."""
    source = await get_state(session_id)
    await check_open(session_id, source.task_flow_id or source.source_session_id)
    messages = fork_messages(source.messages, message_index)
    api = await state_at_message(
        session_id, source.messages, message_index,
        input_type=ApiDesignState,
        default_input=None,
        activity="api_design_turn",
        key="api",
    )
    fork_id, flow_id = await _start(ApiDesignState(
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
    ), source)
    logger.info("Forked API design session %s after message %d as %s",
                session_id, message_index, fork_id)
    return fork_id, flow_id


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
    client = await connect_temporal()
    handle = client.get_workflow_handle(session_id)
    state = await handle.query("get_state", result_type=ApiDesignState)
    if state.pending:
        raise SessionNotFound("This API design session has not started yet.")
    return state


async def get_session(session_id: str) -> dict[str, Any]:
    state = await get_state(session_id)
    session = dataclasses.asdict(state)
    # Polled every few seconds; the requirement list is enough for the page.
    for key in ("requirements_document", "change_document", "contract", "pending",
                "linked_task_flows"):
        del session[key]
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
