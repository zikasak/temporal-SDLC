"""Requirements sessions: one long-running Temporal workflow per interview."""

import dataclasses
import logging
import uuid
from typing import Any

from app.config import TASK_QUEUE
from app.delivery import service as delivery_service
from app.delivery.models import DeliveryProgress, RetryStage
from app.github.models import RepositorySelection
from app.requirements.models import DIRECT_KINDS, RequirementsState
from app.requirements.prompt import OPENING_MESSAGE
from app.temporal.client import connect_temporal
from app.temporal.sessions import SessionBusy, fork_messages, start_update, state_at_message

logger = logging.getLogger(__name__)


async def start_session(
    state: RequirementsState | None = None, *, repository: RepositorySelection | None = None,
) -> str:
    """Start a new interview in `repository`, or a prepared `state` such as a fork."""
    if state is None:
        if repository is None:
            raise ValueError("A requirements session needs a repository.")
        state = RequirementsState(
            messages=[{"role": "assistant", "content": OPENING_MESSAGE}], repository=repository,
        )
    session_id = f"requirements-session-{uuid.uuid4().hex}"
    client = await connect_temporal()
    handle = await client.start_workflow(
        "RequirementsWorkflow",
        state,
        id=session_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started requirements session %s", session_id)
    return handle.id


async def fork_session(session_id: str, message_index: int) -> str:
    """A new session with the conversation up to the message, and the
    requirements as they were at that point."""
    source = await get_state(session_id)
    messages = fork_messages(source.messages, message_index)
    requirements = await state_at_message(
        session_id, source.messages, message_index,
        input_type=RequirementsState | None,
        default_input=RequirementsState(
            messages=[{"role": "assistant", "content": OPENING_MESSAGE}]
        ),
        activity="requirements_turn",
        key="requirements",
    )
    fork_id = await start_session(RequirementsState(
        messages=messages,
        requirements=requirements,
        # The choice is kept even when the fork point precedes it; the user can change it.
        kind=source.kind,
        retired=source.retired,
        repository=source.repository,
        forked_from={"session_id": session_id, "message_index": message_index},
    ))
    logger.info("Forked requirements session %s after message %d as %s",
                session_id, message_index, fork_id)
    return fork_id


async def send_message(session_id: str, message: str) -> None:
    await start_update(session_id, "send_message", message)
    logger.info("Sent message to requirements session %s", session_id)


async def retry_message(session_id: str) -> None:
    await start_update(session_id, "retry_message")
    logger.info("Retrying message in requirements session %s", session_id)


async def finalize_session(session_id: str) -> None:
    await start_update(session_id, "finalize")
    logger.info("Finalizing requirements session %s", session_id)


async def get_state(session_id: str) -> RequirementsState:
    client = await connect_temporal()
    handle = client.get_workflow_handle(session_id)
    return await handle.query("get_state", result_type=RequirementsState)


def _delivers(state: RequirementsState) -> bool:
    """Whether the session's own delivery builds its task, with no API design between."""
    return state.status == "completed" and state.kind in DIRECT_KINDS


async def retry_generation(session_id: str, stage: RetryStage) -> None:
    if not _delivers(await get_state(session_id)):
        raise SessionBusy('Only failed generation can be retried.')
    await delivery_service.retry(session_id, stage)


async def get_session(session_id: str) -> dict[str, Any]:
    state = await get_state(session_id)
    session = dataclasses.asdict(state)
    # Polled every few seconds; the page shows the task, not the repository's contract.
    del session["contract"]
    delivery = None
    if _delivers(state):
        delivery = dataclasses.asdict(
            await delivery_service.get_progress(session_id)
            or DeliveryProgress(verification_status='waiting', baseline_status='waiting'))
    return {"session_id": session_id, **session, "delivery": delivery}
