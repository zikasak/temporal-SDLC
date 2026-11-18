"""Architecture sessions: one long-running Temporal workflow per task's architecture,
started by its task flow once the requirements are finalized, or as the first stage of a
new task flow by a fork; there is no other way to start one."""

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
from app.tasks.models import FORK_SUFFIX, TaskFlowState, with_suffix
from app.tasks.service import start_task_flow, task_flow_closed, task_flow_name
from app.temporal.client import connect_temporal
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


async def _start(state: ArchitectureState, source_task_flow_id: str | None) -> dict[str, str]:
    """Start the session as the first stage of a new top-level task flow named after the
    task it came from with a " (fork)" suffix: its API design and delivery are created
    pending, and it links to that task's requirements session."""
    client = await connect_temporal()
    flow = TaskFlowState(
        kind=state.kind, repository=state.repository,
        name=with_suffix(await task_flow_name(source_task_flow_id, client), FORK_SUFFIX)
        if source_task_flow_id else "",
        task=_requirements_text(state), suffix=FORK_SUFFIX, origin="fork",
        source_task_flow_id=source_task_flow_id,
        linked=[state.source_session_id] if state.source_session_id else [],
        architecture=state)
    task_flow_id = await start_task_flow(flow, "architecture", task_queue=TASK_QUEUE,
                                         client=client)
    session_id = flow.architecture_session_id
    logger.info("Started architecture session %s from %s in task flow %s", session_id,
                state.source_session_id, task_flow_id)
    return {"session_id": session_id, "task_flow_id": task_flow_id}


def _requirements_text(state: ArchitectureState) -> str:
    return "\n".join(f"- {item['id']}: {item['title']}" for item in state.requirements)


async def fork_session(session_id: str, message_index: int) -> str:
    """A new architecture session in a task flow of its own, as fork_task_session
    starts it."""
    return (await fork_task_session(session_id, message_index))["session_id"]


async def fork_task_session(session_id: str, message_index: int) -> dict[str, str]:
    """A new architecture session of the same requirements with the conversation up to
    the message, and the draft as it was at that point, in a new task flow of its own;
    409 once this session's API design has started or its flow has closed."""
    source = await get_state(session_id)
    if source.api_design_session_id:
        raise SessionBusy(DESIGN_STARTED)
    if (await task_flow_closed(source.task_flow_id, await connect_temporal())
            if source.task_flow_id
            else source.source_session_id and await flow_closed(source.source_session_id)):
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
    started = await _start(dataclasses.replace(
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
        task_flow_id=None,
        next_session_id=None,
        pending=False,
    ), source.task_flow_id)
    logger.info("Forked architecture session %s after message %d as %s",
                session_id, message_index, started["session_id"])
    return started


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
    # The design's start and the repository's architecture are internal hand-over state,
    # as is what the task flow reserved.
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
