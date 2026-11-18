"""API design sessions: one long-running Temporal workflow per discussion."""

import dataclasses
import logging
from typing import Any

from app.api_design.models import ApiDesignState
from app.api_design.openapi import design_problems
from app.requirements.lookup import source_session_lookup
from app.delivery import service as delivery_service
from app.delivery.models import DeliveryProgress, RetryStage
from app.config import TASK_QUEUE
from app.tasks.models import FORK_SUFFIX, TaskFlowState, with_suffix
from app.tasks.service import start_task_flow, task_flow_closed, task_flow_name
from app.temporal.client import connect_temporal
from app.temporal.sessions import (
    FLOW_CLOSED, SessionBusy, flow_closed, fork_messages, start_update, state_at_message,
)

logger = logging.getLogger(__name__)


async def _start(state: ApiDesignState, source_task_flow_id: str | None) -> dict[str, str]:
    """Start the design as the first stage of a new top-level task flow named after the
    task it came from with a " (fork)" suffix: its delivery is created pending, and it
    links to that task's requirements and architecture sessions."""
    linked = [item for item in (state.source_session_id, state.architecture_session_id)
              if item]
    client = await connect_temporal()
    flow = TaskFlowState(
        kind=state.kind or "new_service", repository=state.repository,
        name=with_suffix(await task_flow_name(source_task_flow_id, client), FORK_SUFFIX)
        if source_task_flow_id else "",
        task="\n".join(f"- {item['id']}: {item['title']}" for item in state.requirements),
        suffix=FORK_SUFFIX, origin="fork", source_task_flow_id=source_task_flow_id,
        linked=linked, design=state)
    task_flow_id = await start_task_flow(flow, "api_design", task_queue=TASK_QUEUE,
                                         client=client)
    session_id = flow.api_design_session_id
    logger.info("Started API design session %s from %s in task flow %s", session_id,
                state.source_session_id, task_flow_id)
    return {"session_id": session_id, "task_flow_id": task_flow_id}


async def fork_session(session_id: str, message_index: int) -> str:
    """A new design in a task flow of its own, as fork_task_session starts it."""
    return (await fork_task_session(session_id, message_index))["session_id"]


async def fork_task_session(session_id: str, message_index: int) -> dict[str, str]:
    """A new session with the discussion up to the message, and the API model as it was
    at that point, in a new task flow of its own; 409 once its flow has completed or been
    cancelled."""
    source = await get_state(session_id)
    if (await task_flow_closed(source.task_flow_id, await connect_temporal())
            if source.task_flow_id
            else source.source_session_id and await flow_closed(source.source_session_id)):
        raise SessionBusy(FLOW_CLOSED)
    messages = fork_messages(source.messages, message_index)
    api = await state_at_message(
        session_id, source.messages, message_index,
        input_type=ApiDesignState,
        default_input=None,
        activity="api_design_turn",
        key="api",
    )
    started = await _start(ApiDesignState(
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
    ), source.task_flow_id)
    logger.info("Forked API design session %s after message %d as %s",
                session_id, message_index, started["session_id"])
    return started


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
    return await handle.query("get_state", result_type=ApiDesignState)


async def get_session(session_id: str) -> dict[str, Any]:
    state = await get_state(session_id)
    session = dataclasses.asdict(state)
    # Polled every few seconds; the requirement list is enough for the page.
    for key in ("requirements_document", "change_document", "contract", "pending"):
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
