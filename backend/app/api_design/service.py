"""API design sessions: one long-running Temporal workflow per discussion."""

import dataclasses
import logging
from typing import Any

from app.api_design.models import ApiDesignState
from app.api_design.openapi import design_problems
from app.requirements import store as requirements_store
from app.requirements.lookup import source_session_lookup
from app.requirements_review.models import review_id
from app.delivery import service as delivery_service
from app.delivery.models import DeliveryProgress, RetryStage
from app.config import TASK_QUEUE
from app.tasks.models import FORK_SUFFIX, TaskFlowState
from app.tasks.service import (
    derived_name, fork_name_source, new_ids, start_task_flow, task_queue_of,
)
from app.temporal.client import connect_temporal
from app.temporal.registry import register
from app.temporal.sessions import (
    FLOW_CLOSED, SessionBusy, SessionNotFound, flow_closed, fork_messages, start_update,
    state_at_message,
)

logger = logging.getLogger(__name__)


async def _start(state: ApiDesignState, original_task_flow_id: str | None,
                 linked: list[str | None], task_queue: str | None = None) -> dict[str, str]:
    """Start the session as the first stage of a new top-level task flow, linked to the
    original task's earlier stages, with its delivery pending."""
    ids = new_ids()
    session_id = ids["api_design_session_id"]
    state.task_flow_id = ids["task_flow_id"]
    client = await connect_temporal()
    name = await derived_name(original_task_flow_id,
                              fork_name_source(state.requirements, "API design"), FORK_SUFFIX,
                              client)
    task_flow_id = await start_task_flow(ids["task_flow_id"], TaskFlowState(
        kind=state.kind, repository=state.repository, name=name, origin="fork",
        requirements_session_id=state.source_session_id or None,
        architecture_session_id=state.architecture_session_id,
        api_design_session_id=session_id,
        api_design=state,
        linked=[item for item in linked if item],
    ), client, task_queue or TASK_QUEUE, session_id)
    await register(session_id)
    logger.info("Started API design session %s from %s in task flow %s", session_id,
                state.source_session_id, task_flow_id)
    return {"session_id": session_id,
            **({"task_flow_id": task_flow_id} if task_flow_id else {})}


async def fork_session(session_id: str, message_index: int) -> str:
    """fork, returning the new session's ID alone."""
    return (await fork(session_id, message_index))["session_id"]


async def fork(session_id: str, message_index: int) -> dict[str, str]:
    """A new session with the discussion up to the message, and the API model as it was
    at that point, in a new top-level task flow of its own linked to the original task's
    earlier stages; 409 once its flow has completed or been cancelled."""
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
    created = await _start(ApiDesignState(
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
    ), source.task_flow_id, [source.source_session_id,
                             review_id(source.source_session_id)
                             if source.source_session_id else None,
                             source.architecture_session_id],
        await task_queue_of(session_id, await connect_temporal()))
    # The fork's task flow overlays the same baseline with a copy of the task's own
    # requirements.
    await requirements_store.fork_flow(source.task_flow_id, created.get("task_flow_id"),
                                       source.repository)
    logger.info("Forked API design session %s after message %d as %s",
                session_id, message_index, created["session_id"])
    return created


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
    del session["requirements_document"], session["change_document"], session["contract"]
    del session["pending"]
    # The pipeline's results belong to the delivery a finished design starts.
    progress = DeliveryProgress()
    if state.status == 'completed' and state.document:
        progress = (await delivery_service.get_progress(session_id)
                    or DeliveryProgress(verification_status='waiting'))
    lookup = await source_session_lookup(
        state.source_session_id, state.requirements,
        (state.contract or {}).get("requirements"), task_flow_id=state.task_flow_id,
        kind=state.kind)
    return {"session_id": session_id, **session, **dataclasses.asdict(progress),
            "problems": design_problems(state.api, state.language),
            "lookup_requirements": lookup}
