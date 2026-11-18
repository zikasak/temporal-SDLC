"""Starting task flows, and what the session services read about them."""

import asyncio
import logging

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app.config import TASK_QUEUE
from app.tasks.models import (
    WORKFLOW_TYPE, StageKind, TaskFlowState, fallback_name, new_task_flow_id, plan_stages,
    stage_ids, with_suffix,
)
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import (
    DELIVERY_MEMO, LINKED_MEMO, STAGES_MEMO, TASK_NAME_ATTRIBUTE, execution_fields,
)
from app.temporal.registry import register

logger = logging.getLogger(__name__)

# How long starting a flow waits for the task flow to have created its first stage, so
# that the session it returns can be read at once.
STAGE_START_POLLS = 100
STAGE_START_POLL_SECONDS = 0.1


async def _client(client: Client | None) -> Client:
    return client if client is not None else await connect_temporal()


async def start_task_flow(state: TaskFlowState, first: StageKind, *,
                          task_flow_id: str | None = None,
                          client: Client | None = None,
                          task_queue: str = TASK_QUEUE) -> str:
    """Start a task flow whose first stage, working at once, is `first` (the state holds
    its start), with every later stage of its kind pending; returns its workflow ID once
    the first stage exists. The stages before `first` are those `state.linked` names."""
    task_flow_id = task_flow_id or new_task_flow_id()
    plan_stages(state, first)
    name = state.name or with_suffix(fallback_name(state.task), state.suffix)
    delivery = next((item.workflow_id for item in state.stages if item.kind == "delivery"),
                    None)
    client = await _client(client)
    await client.start_workflow(
        WORKFLOW_TYPE, state, id=task_flow_id, task_queue=task_queue,
        **execution_fields(state.repository, name=name, memo={
            STAGES_MEMO: stage_ids(state), DELIVERY_MEMO: delivery,
            LINKED_MEMO: list(state.linked)}))
    await register(task_flow_id)
    first_stage = state.stages[0].workflow_id
    if first != "delivery":
        await register(first_stage)
    await wait_created(first_stage, client)
    logger.info("Started task flow %s with its %s stage %s", task_flow_id, first, first_stage)
    return task_flow_id


async def wait_created(workflow_id: str, client: Client | None = None) -> None:
    """Wait a little for the task flow to have created the stage."""
    client = await _client(client)
    for _ in range(STAGE_START_POLLS):
        try:
            await client.get_workflow_handle(workflow_id).describe()
            return
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
        await asyncio.sleep(STAGE_START_POLL_SECONDS)
    logger.warning("Stage %s was not created in time", workflow_id)


async def task_flow_name(task_flow_id: str | None, client: Client | None = None) -> str:
    """The current name of the task flow; empty when there is none or it is unknown."""
    if not task_flow_id:
        return ""
    client = await _client(client)
    try:
        description = await client.get_workflow_handle(task_flow_id).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return ""
    attributes = description.typed_search_attributes
    return (attributes.get(TASK_NAME_ATTRIBUTE) if attributes else None) or ""


async def task_flow_stages(task_flow_id: str | None,
                           client: Client | None = None) -> list[str]:
    """The workflow IDs of the task flow's own stages; empty when it is unknown."""
    if not task_flow_id:
        return []
    client = await _client(client)
    try:
        description = await client.get_workflow_handle(task_flow_id).describe()
        stages = await description.memo_value(STAGES_MEMO, [])
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return []
    return [item for item in stages or [] if isinstance(item, str)]


async def task_flow_closed(task_flow_id: str | None, client: Client | None = None) -> bool:
    """Whether the task flow completed or was cancelled, as it does once its delivery's
    pull request was merged or closed."""
    if not task_flow_id:
        return False
    client = await _client(client)
    try:
        description = await client.get_workflow_handle(task_flow_id).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return False
    return description.status in (WorkflowExecutionStatus.COMPLETED,
                                  WorkflowExecutionStatus.CANCELED)
