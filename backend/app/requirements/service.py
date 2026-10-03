"""Requirements sessions: one long-running Temporal workflow per interview."""

import dataclasses
import logging
import uuid
from typing import Any

from app.config import TASK_QUEUE
from app.requirements.models import RequirementsState
from app.requirements.prompt import OPENING_MESSAGE
from app.temporal.client import connect_temporal
from app.temporal.sessions import fork_messages, start_update, state_at_message

logger = logging.getLogger(__name__)


async def start_session(state: RequirementsState | None = None) -> str:
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


async def get_session(session_id: str) -> dict[str, Any]:
    state = await get_state(session_id)
    return {"session_id": session_id, **dataclasses.asdict(state)}
