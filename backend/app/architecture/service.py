"""Architecture sessions: one long-running Temporal workflow per task's architecture,
started by its task flow once the requirements are finalized (before task flows existed,
by its requirements session), or as a fork of one; there is no other way to start one."""

import dataclasses
import logging
import uuid
from typing import Any

from temporalio.client import WorkflowUpdateStage

from app.architecture.models import (
    FINISHED_STATUSES, SKIP_PROPOSAL_KEY, ArchitectureState, architecture_problems,
    render_architecture_markdown,
)
from app.config import TASK_QUEUE
from app.requirements.lookup import source_session_lookup
from app.task_flow import service as task_flows
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import execution_fields
from app.temporal.registry import register
from app.temporal.sessions import (
    FLOW_CLOSED, SessionBusy, SessionNotFound, flow_closed, fork_messages, is_running,
    start_update, state_at_message,
)

logger = logging.getLogger(__name__)

WORKFLOW_TYPE = "ArchitectureWorkflow"
TURN_ACTIVITY = "architecture_turn"
SKIP_OPEN = ("Confirm skipping the architecture stage, or decline it by saying what the "
             "architecture should cover.")
DESIGN_STARTED = "The API design of this architecture has started; it can no longer be forked."
NOT_STARTED = "This architecture session has not started yet."


def new_session_id() -> str:
    return f"architecture-session-{uuid.uuid4().hex}"


async def get_state(session_id: str) -> ArchitectureState:
    client = await connect_temporal()
    handle = client.get_workflow_handle(session_id)
    return await handle.query("get_state", result_type=ArchitectureState)


async def _start(state: ArchitectureState) -> str:
    """Start the session as a child of its requirements session, so the task stays one
    workflow tree; a requirements session closed since can only have it on its own."""
    session_id = new_session_id()
    if await is_running(state.source_session_id):
        await start_update(state.source_session_id, "start_architecture", session_id, state,
                           wait_for_stage=WorkflowUpdateStage.COMPLETED)
    else:
        client = await connect_temporal()
        await client.start_workflow(WORKFLOW_TYPE, state, id=session_id, task_queue=TASK_QUEUE,
                                    **execution_fields(state.repository))
    await register(session_id)
    logger.info("Started architecture session %s from %s", session_id, state.source_session_id)
    return session_id


async def fork_session(session_id: str, message_index: int) -> tuple[str, str | None]:
    """A new architecture session of the same requirements session with the conversation
    up to the message, and the draft as it was at that point, in a new task flow of its
    own linked to the original task's requirements session; 409 once this session's API
    design has started, or once its flow has closed. The fork's ID and its task flow's,
    None for a session started before task flows existed."""
    source = await get_state(session_id)
    if source.pending:
        raise SessionBusy(NOT_STARTED)
    if source.api_design_session_id:
        raise SessionBusy(DESIGN_STARTED)
    flow = source.task_flow_id or source.source_session_id
    if flow and await flow_closed(flow):
        raise SessionBusy(FLOW_CLOSED)
    messages = fork_messages(source.messages, message_index)
    structure = await state_at_message(
        session_id, source.messages, message_index,
        input_type=ArchitectureState, default_input=ArchitectureState(),
        activity=TURN_ACTIVITY, key="structure",
    )
    proposals = [index for index, item in enumerate(messages) if item.get(SKIP_PROPOSAL_KEY)]
    open_proposal = None
    if proposals and proposals[-1] == len(messages) - 1:
        open_proposal = {"reason": str(messages[-1][SKIP_PROPOSAL_KEY]),  # type: ignore[literal-required]
                         "message_index": proposals[-1]}
    fork = dataclasses.replace(
        source,
        messages=messages,
        structure=structure,
        skip_proposal=open_proposal,
        # A proposal the kept conversation went on from was declined.
        skip_declined=bool(proposals) and open_proposal is None,
        status="drafting",
        document=None,
        error=None,
        forked_from={"session_id": session_id, "message_index": message_index},
        checkout=None,
        api_design_session_id=None,
        next_session_id=None,
        linked_task_flows=[],
        task_flow_id=None,
    )
    if source.task_flow_id:
        fork_id, task_flow_id = await task_flows.fork_architecture(source, fork)
    else:
        fork_id, task_flow_id = await _start(fork), None
    logger.info("Forked architecture session %s after message %d as %s",
                session_id, message_index, fork_id)
    return fork_id, task_flow_id


async def send_message(session_id: str, message: str) -> None:
    await start_update(session_id, "send_message", message)
    logger.info("Sent message to architecture session %s", session_id)


async def answer_questions(session_id: str, answers: list[str]) -> None:
    await start_update(session_id, "answer_questions", answers)
    logger.info("Sent answers to architecture session %s", session_id)


async def retry_message(session_id: str) -> str:
    """Retry the failed turn; returns the status it leads to."""
    state = await get_state(session_id)
    await start_update(session_id, "retry_message")
    logger.info("Retrying the last turn of architecture session %s", session_id)
    return "drafting" if state.structure is None else "turn_running"


async def finalize_session(session_id: str) -> None:
    await start_update(session_id, "finalize")
    logger.info("Finalizing architecture session %s", session_id)


async def skip_stage(session_id: str) -> None:
    await start_update(session_id, "skip")
    logger.info("Skipping the architecture stage of session %s", session_id)


def problems(state: ArchitectureState) -> list[str]:
    if state.status in FINISHED_STATUSES:
        return []
    found = architecture_problems(state.structure)
    if state.skip_proposal is not None:
        found = [SKIP_OPEN, *found]
    return found


async def get_session(session_id: str) -> dict[str, Any]:
    state = await get_state(session_id)
    if state.pending:
        # Created by its task flow, which has not started it yet.
        raise SessionNotFound(NOT_STARTED)
    session = dataclasses.asdict(state)
    # The design's start and the repository's architecture are internal hand-over state.
    for key in ("design", "requirements_document", "change_document",
                "existing_architecture", "existing_structure", "next_session_id",
                "linked_task_flows", "pending"):
        del session[key]
    structure = session.pop("structure")
    return {
        "session_id": session_id, **session,
        "structure": structure,
        "architecture": render_architecture_markdown(structure) if structure else None,
        "problems": problems(state),
        "lookup_requirements": await source_session_lookup(state.source_session_id,
                                                           state.requirements),
    }
