"""Requirements sessions: one long-running Temporal workflow per interview."""

import dataclasses
import logging
import uuid
from typing import Any

from app.delivery import service as delivery_service
from app.delivery.models import DeliveryProgress, RetryStage
from app.github.models import RepositorySelection
from app.requirements.lookup import requirements_session_lookup
from app.requirements.models import DIRECT_KINDS, RequirementsState, TaskKind
from app.requirements.prompt import intake_messages, opening_message
from app.tasks.models import TaskFlowState, started_id
from app.tasks.service import get_task_state, start_fork, start_task_flow
from app.temporal.client import connect_temporal
from app.temporal.registry import register
from app.temporal.sessions import (
    FLOW_CLOSED, SessionBusy, flow_closed, fork_messages, start_update, state_at_message,
)

logger = logging.getLogger(__name__)


def new_session_id() -> str:
    return f"requirements-session-{uuid.uuid4().hex}"


async def start_session(
    state: RequirementsState | None = None, *, repository: RepositorySelection | None = None,
    kind: TaskKind | None = None, answers: list[str] | None = None, task: str = "",
) -> str:
    """Start a new interview in `repository` from the user's answers to the intake
    questions of `kind` and their `task`, as the requirements child of a new top-level
    task flow; its session ID. `start_task` also returns the task flow's ID."""
    session_id, _ = await start_task(state, repository=repository, kind=kind,
                                     answers=answers, task=task)
    return session_id


async def start_task(
    state: RequirementsState | None = None, *, repository: RepositorySelection | None = None,
    kind: TaskKind | None = None, answers: list[str] | None = None, task: str = "",
) -> tuple[str, str]:
    """Start a new task flow whose requirements child interviews the user from the intake
    (or from a prepared `state`); the session's ID and the task flow's."""
    if state is None:
        if repository is None:
            raise ValueError("A requirements session needs a repository.")
        if kind is None or answers is None or not task.strip():
            raise ValueError("A requirements session starts from the intake questions.")
        state = RequirementsState(messages=intake_messages(kind, answers, task), kind=kind,
                                  repository=repository)
    session_id = new_session_id()
    task_flow_id, _ = await start_task_flow(TaskFlowState(
        kind=state.kind, repository=state.repository, task=task.strip(),
        first_stage="requirements", first_session_id=session_id, requirements=state),
        client=await connect_temporal())
    await register(session_id)
    logger.info("Started requirements session %s in task flow %s", session_id, task_flow_id)
    return session_id, task_flow_id


async def check_flow_open(session_id: str, task_flow_id: str | None) -> None:
    """SessionBusy (409) once the flow the session belongs to has completed or been
    cancelled: its task flow, or the session itself before task flows existed."""
    if await flow_closed(task_flow_id or session_id):
        raise SessionBusy(FLOW_CLOSED)


async def fork_session(session_id: str, message_index: int) -> str:
    """A new session with the conversation up to the message, and the requirements as
    they were at that point, in a new task flow of its own; 409 once the session's flow
    has completed or been cancelled. `fork_task` also returns the task flow's ID."""
    fork_id, _ = await fork_task(session_id, message_index)
    return fork_id


async def fork_task(session_id: str, message_index: int) -> tuple[str, str]:
    source = await get_state(session_id)
    await check_flow_open(session_id, source.task_flow_id)
    messages = fork_messages(source.messages, message_index)
    requirements = await state_at_message(
        session_id, source.messages, message_index,
        input_type=RequirementsState | None,
        default_input=RequirementsState(messages=[opening_message()]),
        activity="requirements_turn",
        key="requirements",
    )
    forked = RequirementsState(
        messages=messages,
        requirements=requirements,
        # The choice is kept even when the fork point precedes it; the user can change it.
        kind=source.kind,
        retired=source.retired,
        repository=source.repository,
        forked_from={"session_id": session_id, "message_index": message_index},
    )
    fork_id = new_session_id()
    task_flow_id = await start_fork(source.task_flow_id, "requirements", fork_id, forked,
                                    kind=forked.kind, repository=forked.repository,
                                    client=await connect_temporal(),
                                    source_session_id=session_id)
    await register(fork_id)
    logger.info("Forked requirements session %s after message %d as %s in task flow %s",
                session_id, message_index, fork_id, task_flow_id)
    return fork_id, task_flow_id


async def send_message(session_id: str, message: str) -> None:
    await start_update(session_id, "send_message", message)
    logger.info("Sent message to requirements session %s", session_id)


async def answer_questions(session_id: str, answers: list[str]) -> None:
    await start_update(session_id, "answer_questions", answers)
    logger.info("Sent answers to requirements session %s", session_id)


async def answer_delivery_questions(session_id: str, workflow_id: str,
                                    answers: list[str]) -> None:
    if not _delivers(await get_state(session_id)):
        raise SessionBusy("This session's delivery asked no questions.")
    await delivery_service.answer(session_id, workflow_id, answers)


async def retry_message(session_id: str) -> None:
    await start_update(session_id, "retry_message")
    logger.info("Retrying message in requirements session %s", session_id)


async def finalize_session(session_id: str) -> None:
    await start_update(session_id, "finalize")
    logger.info("Finalizing requirements session %s", session_id)


async def update_checkout(session_id: str) -> None:
    await start_update(session_id, "update_checkout")
    logger.info("Updating the checkout of requirements session %s", session_id)


async def get_state(session_id: str) -> RequirementsState:
    client = await connect_temporal()
    handle = client.get_workflow_handle(session_id)
    return await handle.query("get_state", result_type=RequirementsState)


def _delivers(state: RequirementsState) -> bool:
    """Whether the session's own delivery builds its task, with no API design between."""
    return state.status == "completed" and state.kind in DIRECT_KINDS


async def retry_generation(session_id: str, stage: RetryStage) -> DeliveryProgress:
    if not _delivers(await get_state(session_id)):
        raise SessionBusy('Only failed generation can be retried.')
    return await delivery_service.retry(session_id, stage)


async def accept_findings(session_id: str, login: str) -> DeliveryProgress:
    if not _delivers(await get_state(session_id)):
        raise SessionBusy('The delivery has no open review findings.')
    return await delivery_service.accept_findings(session_id, login)


async def instruct_findings(session_id: str, instructions: str) -> DeliveryProgress:
    if not _delivers(await get_state(session_id)):
        raise SessionBusy('The delivery has no open review findings.')
    return await delivery_service.instruct_findings(session_id, instructions)


async def get_session(session_id: str) -> dict[str, Any]:
    state = await get_state(session_id)
    session = dataclasses.asdict(state)
    # Polled every few seconds; the page shows the task, not the repository's contract.
    # The API design is found through the architecture session.
    del session["contract"], session["api_design_session_id"]
    if state.task_flow_id:
        # The task flow starts the architecture session; it says which, once it has.
        task = await get_task_state(state.task_flow_id, await connect_temporal())
        session["architecture_session_id"] = (
            started_id(task, "architecture") if task and state.status == "completed"
            else None)
    delivery = None
    if _delivers(state):
        delivery = dataclasses.asdict(
            await delivery_service.get_progress(session_id)
            or DeliveryProgress(
                verification_status='waiting',
                baseline_status='waiting' if state.kind == 'bugfix' else 'not_applicable',
                routing_status='waiting' if state.kind == 'bugfix' else 'not_applicable'))
    return {"session_id": session_id, **session,
            "lookup_requirements": await requirements_session_lookup(session_id, state),
            "delivery": delivery}
