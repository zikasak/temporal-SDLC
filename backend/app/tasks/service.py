"""Task flows: started by a new requirements interview, by a fork of a requirements,
architecture or API design session, and by a fix round of a closed task's delivery."""

import asyncio
import logging
import uuid
from typing import Any

from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.config import TASK_QUEUE
from app.tasks.models import (
    FIX_SUFFIX, FORK_SUFFIX, ID_PREFIX, STAGES, WORKFLOW_TYPE, LinkedStage, Stage,
    TaskFlowState, fallback_name, with_suffix,
)
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import LINKED_MEMO, NAME_MEMO, execution_fields
from app.temporal.registry import register
from app.temporal.sessions import SessionBusy

logger = logging.getLogger(__name__)

# How long starting a task flow waits for it to create its first children.
CREATED_TIMEOUT_SECONDS = 30


def new_task_flow_id() -> str:
    return f"{ID_PREFIX}{uuid.uuid4().hex}"


async def task_queue_of(client: Client, workflow_id: str | None) -> str | None:
    """The task queue `workflow_id` runs on, where a flow started from it runs too; None
    when it cannot be told, such as when it does not exist."""
    if not workflow_id:
        return None
    try:
        description = await client.get_workflow_handle(workflow_id).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return None
    return description.task_queue or None


async def start_task_flow(state: TaskFlowState, workflow_id: str | None = None,
                          client: Client | None = None,
                          task_queue: str | None = None) -> tuple[str, bool]:
    """Start the task flow and wait until it created its first children, so that the
    session it starts with can be reached. A `workflow_id` already started, such as by a
    handover tried again, is waited for instead. Its ID, and whether this call started it.
    It runs on `task_queue`, by default the service's own."""
    client = client or await connect_temporal()
    workflow_id = workflow_id or new_task_flow_id()
    if not state.name:
        state.name = fallback_name(state.task)
    memo = {NAME_MEMO: state.name,
            LINKED_MEMO: [item.workflow_id for item in state.linked]}
    started = True
    try:
        handle = await client.start_workflow(
            WORKFLOW_TYPE, state, id=workflow_id, task_queue=task_queue or TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            **execution_fields(state.repository, memo_fields=memo),
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
        started = False
    try:
        await asyncio.wait_for(handle.execute_update("wait_created"), CREATED_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        # No worker took the flow up yet: it creates its children once one does.
        logger.warning("Task flow %s has not created its children yet", workflow_id)
    except WorkflowUpdateFailedError as exc:
        raise SessionBusy(str(exc.cause or exc)) from exc
    except RPCError as exc:
        # A flow that has closed already created its children.
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
    if started:
        await register(workflow_id)
        logger.info("Started task flow %s with its %s %s", workflow_id, state.first_stage,
                    state.first_session_id)
    return workflow_id, started


async def get_task_state(task_flow_id: str | None,
                         client: Client | None = None) -> TaskFlowState | None:
    """The task flow's state; None without a task flow or when it does not exist."""
    if not task_flow_id:
        return None
    client = client or await connect_temporal()
    try:
        return await client.get_workflow_handle(task_flow_id).query(
            "get_state", result_type=TaskFlowState)
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return None


def earlier_stages(original: TaskFlowState | None, before: Stage | None) -> list[LinkedStage]:
    """The stages of the original task a new task flow links to: those it links to itself
    and its own children of the stages before `before` (all of them when None) that
    began, in order."""
    if original is None:
        return []
    allowed = STAGES[:STAGES.index(before)] if before else STAGES
    linked = [item for item in original.linked if item.stage in allowed]
    seen = {item.workflow_id for item in linked}
    for record in original.stages:
        if (record.stage in allowed and record.status not in ("pending", "skipped")
                and record.workflow_id not in seen):
            linked.append(LinkedStage(stage=record.stage, workflow_id=record.workflow_id))
            seen.add(record.workflow_id)
    return linked


async def start_fork(source_task_flow_id: str | None, stage: Stage, session_id: str,
                     state: Any, *, kind: Any, repository: Any,
                     task: str = "", client: Client | None = None,
                     source_session_id: str | None = None) -> str:
    """Start the task flow of a fork of the `stage` session: named after the original with
    a " (fork)" suffix, holding the forked session `session_id`, linking to the original
    task's earlier stages and with pending children for the stages after it. It runs on
    the task queue of the original task flow, or of the forked `source_session_id`. Its ID."""
    client = client or await connect_temporal()
    original = await get_task_state(source_task_flow_id, client)
    task_queue = (await task_queue_of(client, source_task_flow_id)
                  or await task_queue_of(client, source_session_id))
    name = with_suffix(original.name, FORK_SUFFIX) if original and original.name else ""
    flow = TaskFlowState(
        kind=kind, repository=repository, task=task or (original.task if original else ""),
        name=name or with_suffix(fallback_name(task or "Task"), FORK_SUFFIX),
        suffix=FORK_SUFFIX, first_stage=stage, first_session_id=session_id,
        linked=earlier_stages(original, stage))
    setattr(flow, stage, state)
    task_flow_id, _ = await start_task_flow(flow, client=client, task_queue=task_queue)
    return task_flow_id


def fix_task_flow_id(follow_up_session_id: str) -> str:
    """The task flow of a fix round's session (fix-session-<hex>): task-flow-<hex>, so that
    a handover tried again finds the flow it started."""
    return ID_PREFIX + follow_up_session_id.rsplit("-", 1)[-1]


async def start_fix(original_task_flow_id: str | None, follow_up_session_id: str,
                    delivery: Any, client: Client | None = None,
                    task_queue: str | None = None) -> tuple[str, bool]:
    """Start the task flow of a fix round of a closed task's delivery: named after the
    original with a " (fix)" suffix, holding the fix delivery as its child and linking to
    the original task's stages. Its ID, and whether this call started it."""
    client = client or await connect_temporal()
    original = await get_task_state(original_task_flow_id, client)
    task_queue = task_queue or await task_queue_of(client, original_task_flow_id)
    name = with_suffix(original.name, FIX_SUFFIX) if original and original.name else ""
    spec = delivery.spec
    flow = TaskFlowState(
        kind=spec.kind, repository=spec.repository,
        task=original.task if original else "",
        name=name or with_suffix(fallback_name(
            (original.task if original else "") or "Fix"), FIX_SUFFIX),
        suffix=FIX_SUFFIX, first_stage="delivery", first_session_id=follow_up_session_id,
        delivery=delivery, delivery_session_id=follow_up_session_id,
        linked=earlier_stages(original, None))
    return await start_task_flow(flow, fix_task_flow_id(follow_up_session_id), client,
                                 task_queue)
