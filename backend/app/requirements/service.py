"""Requirements sessions: one long-running Temporal workflow per interview."""

import dataclasses
import logging
from typing import Any

from app.delivery import service as delivery_service
from app.delivery.models import DeliveryProgress, RetryStage
from app.github.models import RepositorySelection
from app.requirements.lookup import requirements_session_lookup
from app.requirements.models import DIRECT_KINDS, RequirementsState, TaskKind
from app.requirements.prompt import intake_messages, opening_message
from app.config import TASK_QUEUE
from app.tasks.models import FORK_SUFFIX, TaskFlowState, with_suffix
from app.tasks.service import start_task_flow, task_flow_closed, task_flow_name
from app.temporal.client import connect_temporal
from app.temporal.sessions import (
    FLOW_CLOSED, SessionBusy, flow_closed, fork_messages, start_update, state_at_message,
)

logger = logging.getLogger(__name__)


async def start_session(
    state: RequirementsState | None = None, *, repository: RepositorySelection | None = None,
    kind: TaskKind | None = None, answers: list[str] | None = None, task: str = "",
) -> str:
    """Start a new interview, as start_task_session does; returns its session ID."""
    started = await start_task_session(state, repository=repository, kind=kind,
                                       answers=answers, task=task)
    return started["session_id"]


async def start_task_session(
    state: RequirementsState | None = None, *, repository: RepositorySelection | None = None,
    kind: TaskKind | None = None, answers: list[str] | None = None, task: str = "",
    name: str = "", suffix: str = "", source_task_flow_id: str | None = None,
) -> dict[str, str]:
    """Start a new top-level task flow whose requirements stage interviews in
    `repository` from the user's answers to the intake questions of `kind` and their
    `task`, or from a prepared `state` such as a fork; its later stages are created
    pending. The flow is named `name`, or by the LLM from `task`, with `suffix`. Returns
    the session's and the task flow's IDs."""
    if state is None:
        if repository is None:
            raise ValueError("A requirements session needs a repository.")
        if kind is None or answers is None or not task.strip():
            raise ValueError("A requirements session starts from the intake questions.")
        state = RequirementsState(messages=intake_messages(kind, answers, task), kind=kind,
                                  repository=repository)
    flow = TaskFlowState(kind=state.kind or "bugfix", repository=state.repository,
                         name=with_suffix(name, suffix) if name else "",
                         task=task or _task_text(state), suffix=suffix,
                         origin="fork" if state.forked_from else "new",
                         source_task_flow_id=source_task_flow_id, requirements=state)
    task_flow_id = await start_task_flow(flow, "requirements", task_queue=TASK_QUEUE,
                                         client=await connect_temporal())
    session_id = flow.requirements_session_id
    logger.info("Started requirements session %s in task flow %s", session_id, task_flow_id)
    return {"session_id": session_id, "task_flow_id": task_flow_id}


def _task_text(state: RequirementsState) -> str:
    """What the session's task is, as its first user message (the intake) says."""
    first = next((item["content"] for item in state.messages if item["role"] == "user"), "")
    return first.rpartition("→ ")[2] if "→ " in first else first


async def fork_session(session_id: str, message_index: int) -> str:
    """A new session in a task flow of its own, as fork_task_session starts it."""
    return (await fork_task_session(session_id, message_index))["session_id"]


async def fork_task_session(session_id: str, message_index: int) -> dict[str, str]:
    """A new session with the conversation up to the message, and the requirements as
    they were at that point, in a new top-level task flow of its own named after the
    original with a " (fork)" suffix and pending stages after it; 409 once the session's
    flow has completed or been cancelled."""
    source = await get_state(session_id)
    client = await connect_temporal()
    if (await task_flow_closed(source.task_flow_id, client) if source.task_flow_id
            else await flow_closed(session_id)):
        raise SessionBusy(FLOW_CLOSED)
    messages = fork_messages(source.messages, message_index)
    requirements = await state_at_message(
        session_id, source.messages, message_index,
        input_type=RequirementsState | None,
        default_input=RequirementsState(messages=[opening_message()]),
        activity="requirements_turn",
        key="requirements",
    )
    started = await start_task_session(
        RequirementsState(
            messages=messages,
            requirements=requirements,
            # The choice is kept even when the fork point precedes it; the user can change it.
            kind=source.kind,
            retired=source.retired,
            repository=source.repository,
            forked_from={"session_id": session_id, "message_index": message_index},
        ),
        name=await task_flow_name(source.task_flow_id, client), suffix=FORK_SUFFIX,
        source_task_flow_id=source.task_flow_id)
    logger.info("Forked requirements session %s after message %d as %s",
                session_id, message_index, started["session_id"])
    return started


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
    """Whether the session's delivery builds its task, with no API design between: its
    own child, or the delivery its task flow starts once the session is finalized. A
    document written because the session was idle starts none."""
    return (state.status == "completed" and state.kind in DIRECT_KINDS
            and not state.stages_skipped)


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
    # The API design is found through the architecture session; the rest is the task
    # flow's bookkeeping.
    for key in ("contract", "api_design_session_id", "next_session_id", "stages_skipped"):
        del session[key]
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
