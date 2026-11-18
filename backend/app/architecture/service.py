"""Architecture sessions: one long-running Temporal workflow per task's architecture,
a stage of the task's top-level task flow, which starts it once the requirements are
finalized; a fork starts a new task flow of its own."""

import dataclasses
import logging
import uuid
from typing import Any

from app.architecture.models import (
    FINISHED_STATUSES, SKIP_PROPOSAL_KEY, ArchitectureState, architecture_problems,
    render_architecture_markdown,
)
from app.requirements.lookup import source_session_lookup
from app.config import TASK_QUEUE
from app.tasks.models import FORK_SUFFIX, TaskFlowState
from app.tasks.service import derived_name, fork_name_source, new_ids, start_task_flow
from app.temporal.client import connect_temporal
from app.temporal.registry import register
from app.temporal.sessions import (
    FLOW_CLOSED, SessionBusy, flow_closed, fork_messages, start_update, state_at_message,
)

logger = logging.getLogger(__name__)

WORKFLOW_TYPE = "ArchitectureWorkflow"
TURN_ACTIVITY = "architecture_turn"
SKIP_OPEN = ("Confirm skipping the architecture stage, or decline it by saying what the "
             "architecture should cover.")
DESIGN_STARTED = "The API design of this architecture has started; it can no longer be forked."


def new_session_id() -> str:
    return f"architecture-session-{uuid.uuid4().hex}"


async def get_state(session_id: str) -> ArchitectureState:
    client = await connect_temporal()
    handle = client.get_workflow_handle(session_id)
    return await handle.query("get_state", result_type=ArchitectureState)


async def _start(state: ArchitectureState, original_task_flow_id: str | None) -> dict[str, str]:
    """Start the session as the first stage of a new top-level task flow, linked to the
    original task's requirements session, with its API design and delivery pending."""
    ids = new_ids()
    session_id = ids["architecture_session_id"]
    state.task_flow_id = ids["task_flow_id"]
    state.next_session_id = ids["api_design_session_id"]
    if state.design is not None:
        state.design = dataclasses.replace(state.design, task_flow_id=ids["task_flow_id"])
    client = await connect_temporal()
    name = await derived_name(original_task_flow_id,
                              fork_name_source(state.requirements, "Architecture"),
                              FORK_SUFFIX, client)
    await start_task_flow(ids["task_flow_id"], TaskFlowState(
        kind=state.kind, repository=state.repository, name=name, origin="fork",
        requirements_session_id=state.source_session_id or None,
        architecture_session_id=session_id,
        api_design_session_id=ids["api_design_session_id"],
        architecture=state,
        linked=[state.source_session_id] if state.source_session_id else [],
    ), client, TASK_QUEUE, session_id)
    await register(session_id)
    logger.info("Started architecture session %s from %s in task flow %s", session_id,
                state.source_session_id, ids["task_flow_id"])
    return {"session_id": session_id, "task_flow_id": ids["task_flow_id"]}


async def fork_session(session_id: str, message_index: int) -> str:
    """fork, returning the new session's ID alone."""
    return (await fork(session_id, message_index))["session_id"]


async def fork(session_id: str, message_index: int) -> dict[str, str]:
    """A new architecture session of the same requirements session with the conversation
    up to the message, and the draft as it was at that point, in a new top-level task flow
    of its own; 409 once this session's API design has started or its flow closed."""
    source = await get_state(session_id)
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
    created = await _start(dataclasses.replace(
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
        pending=False,
    ), source.task_flow_id)
    logger.info("Forked architecture session %s after message %d as %s",
                session_id, message_index, created["session_id"])
    return created


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
    session = dataclasses.asdict(state)
    # The design's start and the repository's architecture are internal hand-over state.
    for key in ("design", "requirements_document", "change_document",
                "existing_architecture", "existing_structure", "next_session_id", "pending"):
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
