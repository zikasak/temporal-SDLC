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
from app.task_flow.models import FORK_SUFFIX, TaskFlowState
from app.task_flow.service import (
    check_open, derived_name, flow_cannot_run, new_stage_id, run_stage_alone, stage_started,
    start_task_flow,
)
from app.temporal.client import connect_temporal
from app.temporal.sessions import (
    SessionBusy, SessionNotFound, fork_messages, start_update, state_at_message,
)

logger = logging.getLogger(__name__)


async def start_task(
    state: RequirementsState | None = None, *, repository: RepositorySelection | None = None,
    kind: TaskKind | None = None, answers: list[str] | None = None, task: str = "",
    name: str = "", name_suffix: str = "", origin: str = "new", task_queue: str | None = None,
) -> tuple[str, str | None]:
    """Start a new top-level task flow whose requirements child interviews the user in
    `repository` from their answers to the intake questions of `kind` and their `task`,
    or from a prepared `state` such as a fork. Returns the session's and the task flow's
    IDs, the latter None when the session runs on its own. `task_queue` is the queue the
    flow runs on, such as the forked session's."""
    if state is None:
        if repository is None:
            raise ValueError("A requirements session needs a repository.")
        if kind is None or answers is None or not task.strip():
            raise ValueError("A requirements session starts from the intake questions.")
        state = RequirementsState(messages=intake_messages(kind, answers, task), kind=kind,
                                  repository=repository)
    session_id = new_stage_id("requirements")
    client = await connect_temporal()
    flow_id = await start_task_flow(TaskFlowState(
        kind=state.kind, repository=state.repository,
        task=task.strip() or _task_text(state), name=name, name_suffix=name_suffix,
        origin=origin,  # type: ignore[arg-type]
        first_stage="requirements", first_stage_id=session_id, requirements=state),
        client=client, task_queue=task_queue)
    if not await stage_started(client, session_id) and await flow_cannot_run(client, flow_id):
        # No worker on the queue runs task flows: the session runs on its own.
        await run_stage_alone(client, flow_id, "RequirementsWorkflow", session_id,
                              dataclasses.replace(state, task_flow_id=None, pending=False),
                              state.repository, task_queue)
        return session_id, None
    logger.info("Started requirements session %s in task flow %s", session_id, flow_id)
    return session_id, flow_id


async def start_session(
    state: RequirementsState | None = None, *, repository: RepositorySelection | None = None,
    kind: TaskKind | None = None, answers: list[str] | None = None, task: str = "",
) -> str:
    """As start_task; returns the session's ID."""
    session_id, _ = await start_task(state, repository=repository, kind=kind,
                                     answers=answers, task=task)
    return session_id


def _task_text(state: RequirementsState) -> str:
    """What the conversation says needs to be done: the user's first message, such as the
    intake's answers, for naming the task."""
    return next((str(item["content"]) for item in state.messages if item["role"] == "user"),
                "")


async def fork_session(session_id: str, message_index: int) -> str:
    """As fork; returns the fork's session ID."""
    fork_id, _ = await fork(session_id, message_index)
    return fork_id


async def fork(session_id: str, message_index: int) -> tuple[str, str | None]:
    """A new session with the conversation up to the message, and the requirements as
    they were at that point, in a new task flow of its own named after the original with
    a " (fork)" suffix; 409 once the session's flow has completed or been cancelled.
    Returns the fork's and its task flow's IDs."""
    source = await get_state(session_id)
    client = await connect_temporal()
    await check_open(session_id, source.task_flow_id, client)
    messages = fork_messages(source.messages, message_index)
    requirements = await state_at_message(
        session_id, source.messages, message_index,
        input_type=RequirementsState | None,
        default_input=RequirementsState(messages=[opening_message()]),
        activity="requirements_turn",
        key="requirements",
    )
    name, suffix = await derived_name(source.task_flow_id, FORK_SUFFIX, client)
    # The fork's flow runs where the forked session does.
    task_queue = (await client.get_workflow_handle(session_id).describe()).task_queue
    fork_id, flow_id = await start_task(RequirementsState(
        messages=messages,
        requirements=requirements,
        # The choice is kept even when the fork point precedes it; the user can change it.
        kind=source.kind,
        retired=source.retired,
        repository=source.repository,
        forked_from={"session_id": session_id, "message_index": message_index},
    ), name=name, name_suffix=suffix, origin="fork", task_queue=task_queue)
    logger.info("Forked requirements session %s after message %d as %s",
                session_id, message_index, fork_id)
    return fork_id, flow_id


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
    state = await handle.query("get_state", result_type=RequirementsState)
    if state.pending:
        raise SessionNotFound("This session has not started yet.")
    return state


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
    for key in ("contract", "api_design_session_id", "pending", "linked_task_flows"):
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
