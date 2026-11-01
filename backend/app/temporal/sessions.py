"""Helpers shared by the long-running, update-driven session workflows."""

import logging
from typing import Any

from temporalio.api.enums.v1 import EventType
from temporalio.client import (
    WorkflowExecutionStatus,
    WorkflowUpdateFailedError,
    WorkflowUpdateStage,
)
from temporalio.service import RPCError, RPCStatusCode

from app.conversations.models import ConversationMessage
from app.temporal.client import connect_temporal

logger = logging.getLogger(__name__)


class SessionBusy(Exception):
    pass


class SessionEnded(SessionBusy):
    """The workflow closed before the update reached it."""


class InvalidForkPoint(ValueError):
    pass


class SessionNotFound(LookupError):
    pass


def fork_messages(
    messages: list[ConversationMessage], message_index: int
) -> list[ConversationMessage]:
    """The transcript a fork keeps: everything up to and including the message."""
    if not 0 <= message_index < len(messages):
        raise InvalidForkPoint("There is no message at that position to fork from.")
    return messages[: message_index + 1]


async def _turn_history(
    session_id: str, input_type: Any, activity: str
) -> tuple[Any, list[dict[str, Any]]]:
    """The session's first input and each completed `activity` result, oldest first.

    Continue-as-new splits a session into several runs, so this walks back
    through all of them."""
    client = await connect_temporal()
    runs: list[list[dict[str, Any]]] = []
    run_id: str | None = None
    while True:
        handle = client.get_workflow_handle(session_id, run_id=run_id)
        scheduled: dict[int, str] = {}
        results: list[dict[str, Any]] = []
        first_input: Any = None
        previous_run_id = ""
        async for event in handle.fetch_history_events():
            if event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
                started = event.workflow_execution_started_event_attributes
                previous_run_id = started.continued_execution_run_id
                if started.input.payloads:
                    [first_input] = await client.data_converter.decode(
                        started.input.payloads, [input_type]
                    )
            elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
                attributes = event.activity_task_scheduled_event_attributes
                scheduled[event.event_id] = attributes.activity_type.name
            elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
                completed = event.activity_task_completed_event_attributes
                if scheduled.get(completed.scheduled_event_id) == activity:
                    [result] = await client.data_converter.decode(
                        completed.result.payloads, [dict]
                    )
                    results.append(result)
        runs.append(results)
        if not previous_run_id:
            return first_input, [item for run in reversed(runs) for item in run]
        run_id = previous_run_id


async def state_at_message(
    session_id: str,
    messages: list[ConversationMessage],
    message_index: int,
    *,
    input_type: Any,
    default_input: Any,
    activity: str,
    key: str,
) -> Any:
    """What the session's `key` field held right after `messages[message_index]`.

    Each turn activity returns the reply it added and the updated field, so the
    workflow history records the field's value at every assistant message."""
    initial, results = await _turn_history(session_id, input_type, activity)
    initial = initial or default_input
    prefix = len(initial.messages)
    if message_index < prefix - 1 and initial.forked_from:
        # This session is a fork; the earlier messages belong to its source.
        return await state_at_message(
            initial.forked_from["session_id"], messages, message_index,
            input_type=input_type, default_input=default_input, activity=activity, key=key,
        )
    value = getattr(initial, key)
    # Failed turns add no message, and fallback replies have no turn, so match
    # each turn to its reply rather than counting.
    turn = 0
    for item in messages[prefix : message_index + 1]:
        if (item["role"] == "assistant" and turn < len(results)
                and results[turn]["reply"] == item["content"]):
            # Results recorded before a field existed leave it as it was.
            value = results[turn].get(key, value)
            turn += 1
    return value


async def start_update(session_id: str, update: str, *args: Any,
                       wait_for_stage: WorkflowUpdateStage = WorkflowUpdateStage.ACCEPTED,
                       update_id: str | None = None) -> None:
    client = await connect_temporal()
    handle = client.get_workflow_handle(session_id)
    try:
        update_handle = await handle.start_update(update, args=list(args), id=update_id,
                                                  wait_for_stage=wait_for_stage)
        # A rejected update returns at once with its outcome known; only its result
        # raises the validator's error.
        if update_handle._known_outcome is not None:
            await update_handle.result()
    except WorkflowUpdateFailedError as exc:
        # The workflow's validator rejected the update, or its handler failed.
        logger.warning("Session %s rejected %s: %s", session_id, update, exc.cause or exc)
        raise SessionBusy(str(exc.cause or exc)) from exc
    except RPCError as exc:
        # Updates to a closed workflow also report NOT_FOUND.
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        description = await handle.describe()
        if description.status == WorkflowExecutionStatus.RUNNING:
            raise
        logger.warning("Session %s has ended; %s rejected", session_id, update)
        raise SessionEnded("This session has ended.") from exc


async def is_running(workflow_id: str) -> bool:
    """Whether the workflow's latest run is open; false when there is none."""
    client = await connect_temporal()
    try:
        description = await client.get_workflow_handle(workflow_id).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return False
    return description.status == WorkflowExecutionStatus.RUNNING
