"""Task flows: one top-level TaskWorkflow per task, started by a new requirements
interview, by a fork of a requirements, architecture or API design session, and by a fix
round on a closed task's delivery."""

import asyncio
import logging
import uuid
from typing import Any

from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from app.config import TASK_QUEUE
from app.task_flow.models import (
    FIX_SUFFIX, FORK_SUFFIX, STAGE_PREFIXES, TASK_FLOW_PREFIX, WORKFLOW_TYPE, TaskFlowState,
)
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import (
    TASK_FLOW_MEMO, TASK_NAME_ATTRIBUTE, execution_fields,
)
from app.temporal.registry import register
from app.temporal.sessions import FLOW_CLOSED, SessionBusy, flow_closed

logger = logging.getLogger(__name__)


def new_task_flow_id() -> str:
    return f"{TASK_FLOW_PREFIX}{uuid.uuid4().hex}"


def new_stage_id(stage: str) -> str:
    return f"{STAGE_PREFIXES[stage]}{uuid.uuid4().hex}"


async def start_task_flow(state: TaskFlowState, flow_id: str | None = None) -> str:
    """Start the task flow, which starts its first stage (`state.first_stage_id`) at once
    and creates the stages after it pending. With `flow_id`, such as a fix round's flow
    derived from the delivery it carries on, a flow of that ID that ran before is not
    started again: WorkflowAlreadyStartedError."""
    flow_id = flow_id or new_task_flow_id()
    client = await connect_temporal()
    await client.start_workflow(
        WORKFLOW_TYPE, state, id=flow_id, task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        **execution_fields(state.repository),
    )
    await register(flow_id)
    if state.first_stage != "delivery" and state.first_stage_id:
        await register(state.first_stage_id)
    if state.first_stage_id:
        await _first_stage_started(client, state.first_stage_id)
    logger.info("Started task flow %s (%s) with its %s stage %s", flow_id, state.origin,
                state.first_stage, state.first_stage_id)
    return flow_id


# How long starting a task flow waits for it to have started its first stage, which the
# caller then reports, such as the session the user opens next.
FIRST_STAGE_POLLS = 100
FIRST_STAGE_POLL_SECONDS = 0.1


async def _first_stage_started(client: Any, workflow_id: str) -> None:
    for _ in range(FIRST_STAGE_POLLS):
        try:
            await client.get_workflow_handle(workflow_id).describe()
            return
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
        await asyncio.sleep(FIRST_STAGE_POLL_SECONDS)
    logger.warning("The first stage %s of a task flow has not started yet", workflow_id)


async def _describe(flow_id: str | None):
    if not flow_id:
        return None
    client = await connect_temporal()
    try:
        return await client.get_workflow_handle(flow_id).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return None


async def task_name(flow_id: str | None) -> str:
    """The task flow's current name; empty when it has none, such as no flow at all."""
    description = await _describe(flow_id)
    if description is None:
        return ""
    return str(description.typed_search_attributes.get(TASK_NAME_ATTRIBUTE) or "")


def _without_suffix(name: str) -> str:
    """A name without the suffixes earlier forks and fix rounds gave it."""
    for suffix in (FORK_SUFFIX, FIX_SUFFIX):
        while name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


async def derived_name(flow_id: str | None, suffix: str) -> tuple[str, str]:
    """The name of a fork or fix round of the task flow `flow_id` and the suffix it keeps
    when renamed; an empty name when the original has none, so that the new flow names
    itself."""
    name = _without_suffix(await task_name(flow_id))
    return (f"{name}{suffix}" if name else "", suffix)


async def stages(flow_id: str | None) -> dict[str, Any]:
    """The task flow's own stages, the earlier stages it links to and its delivery, as its
    memo records them: {"stages": [...], "linked": [...], "delivery": ...}."""
    description = await _describe(flow_id)
    if description is None:
        return {}
    try:
        value = await description.memo_value(TASK_FLOW_MEMO, None)
    except Exception:  # noqa: BLE001 - an unreadable memo lists as no stages
        logger.warning("Unreadable stages of task flow %s", flow_id, exc_info=True)
        return {}
    return dict(value) if isinstance(value, dict) else {}


async def check_open(session_id: str, task_flow_id: str | None) -> None:
    """SessionBusy (409) when the session's flow has completed or been cancelled: its
    task flow, or for a session from before task flows the session itself."""
    for workflow_id in (task_flow_id, session_id):
        if workflow_id and await flow_closed(workflow_id):
            raise SessionBusy(FLOW_CLOSED)


async def is_open(flow_id: str) -> bool:
    description = await _describe(flow_id)
    return bool(description and description.status == WorkflowExecutionStatus.RUNNING)
