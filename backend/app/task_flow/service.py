"""Task flows: one top-level TaskWorkflow per task, started by a new requirements
interview, by a fork of a requirements, architecture or API design session, and by a fix
round on a closed task's delivery."""

import asyncio
import logging
import uuid
from typing import Any

from temporalio.api.enums.v1 import EventType
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


async def start_task_flow(state: TaskFlowState, flow_id: str | None = None, *,
                          client: Any = None, task_queue: str | None = None) -> str:
    """Start the task flow, which starts its first stage (`state.first_stage_id`) at once
    and creates the stages after it pending. With `flow_id`, such as a fix round's flow
    derived from the delivery it carries on, a flow of that ID that ran before is not
    started again: WorkflowAlreadyStartedError. `client` is the caller's connection to
    Temporal, and `task_queue` the queue of the workflows the flow derives from, such as
    the forked session's; by default this service's own."""
    flow_id = flow_id or new_task_flow_id()
    client = client or await connect_temporal()
    await client.start_workflow(
        WORKFLOW_TYPE, state, id=flow_id, task_queue=task_queue or TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        **execution_fields(state.repository),
    )
    await register(flow_id)
    if state.first_stage != "delivery" and state.first_stage_id:
        await register(state.first_stage_id)
    if state.first_stage_id:
        await stage_started(client, state.first_stage_id)
    logger.info("Started task flow %s (%s) with its %s stage %s", flow_id, state.origin,
                state.first_stage, state.first_stage_id)
    return flow_id


# How long starting a task flow waits for it to have started its first stage, which the
# caller then reports, such as the session the user opens next. The wait is best effort
# and bounded: a stage that has not started by then is reported all the same.
FIRST_STAGE_WAIT_SECONDS = 2.0
FIRST_STAGE_POLL_SECONDS = 0.05


async def stage_started(client: Any, workflow_id: str) -> bool:
    """Whether the stage `workflow_id` has been started, waiting a bounded while for it;
    True too when that cannot be checked."""
    try:
        async with asyncio.timeout(FIRST_STAGE_WAIT_SECONDS):
            while True:
                try:
                    await client.get_workflow_handle(workflow_id).describe()
                    return True
                except RPCError as exc:
                    if exc.status != RPCStatusCode.NOT_FOUND:
                        logger.warning("Could not check the first stage %s: %s",
                                       workflow_id, exc)
                        return True
                await asyncio.sleep(FIRST_STAGE_POLL_SECONDS)
    except TimeoutError:
        logger.warning("The first stage %s of a task flow has not started yet", workflow_id)
        return False


async def flow_cannot_run(client: Any, flow_id: str) -> bool:
    """Whether the task flow's workflow tasks fail, such as on a queue whose worker does
    not run task flows, waiting a bounded while to find out; False while no worker has
    tried to run it, as when the worker is down, so that the flow runs once it is up."""
    try:
        async with asyncio.timeout(FIRST_STAGE_WAIT_SECONDS):
            while True:
                handle = client.get_workflow_handle(flow_id)
                async for event in handle.fetch_history_events():
                    if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED:
                        return True
                    if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED:
                        return False
                await asyncio.sleep(FIRST_STAGE_POLL_SECONDS * 4)
    except TimeoutError:
        return False
    except RPCError as exc:
        logger.warning("Could not check task flow %s: %s", flow_id, exc)
        return False


async def run_stage_alone(client: Any, flow_id: str, workflow_type: str, workflow_id: str,
                          start: Any, repository: Any, task_queue: str | None) -> None:
    """Run a first stage that its task flow has not started, such as when no worker runs
    task flows on the queue, as a session of its own: the task flow is stopped and the
    stage started directly, so that the session the caller reports exists."""
    try:
        await client.get_workflow_handle(flow_id).terminate(
            "The task flow did not start its first stage; it runs on its own.")
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
    await client.start_workflow(
        workflow_type, start, id=workflow_id, task_queue=task_queue or TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.TERMINATE_EXISTING,
        **execution_fields(repository),
    )
    logger.warning("Started stage %s on its own: task flow %s did not start it",
                   workflow_id, flow_id)


async def _describe(flow_id: str | None, client: Any = None):
    if not flow_id:
        return None
    client = client or await connect_temporal()
    try:
        return await client.get_workflow_handle(flow_id).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return None


async def task_name(flow_id: str | None, client: Any = None) -> str:
    """The task flow's current name; empty when it has none, such as no flow at all."""
    description = await _describe(flow_id, client)
    if description is None:
        return ""
    return str(description.typed_search_attributes.get(TASK_NAME_ATTRIBUTE) or "")


def _without_suffix(name: str) -> str:
    """A name without the suffixes earlier forks and fix rounds gave it."""
    for suffix in (FORK_SUFFIX, FIX_SUFFIX):
        while name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


async def derived_name(flow_id: str | None, suffix: str,
                       client: Any = None) -> tuple[str, str]:
    """The name of a fork or fix round of the task flow `flow_id` and the suffix it keeps
    when renamed; an empty name when the original has none, so that the new flow names
    itself."""
    name = _without_suffix(await task_name(flow_id, client))
    return (f"{name}{suffix}" if name else "", suffix)


async def stages(flow_id: str | None, client: Any = None) -> dict[str, Any]:
    """The task flow's own stages, the earlier stages it links to and its delivery, as its
    memo records them: {"stages": [...], "linked": [...], "delivery": ...}."""
    description = await _describe(flow_id, client)
    if description is None:
        return {}
    try:
        value = await description.memo_value(TASK_FLOW_MEMO, None)
    except Exception:  # noqa: BLE001 - an unreadable memo lists as no stages
        logger.warning("Unreadable stages of task flow %s", flow_id, exc_info=True)
        return {}
    return dict(value) if isinstance(value, dict) else {}


async def check_open(session_id: str, task_flow_id: str | None,
                     client: Any = None) -> None:
    """SessionBusy (409) when the session's flow has completed or been cancelled: its
    task flow, or for a session from before task flows the session itself."""
    for workflow_id in (task_flow_id, session_id):
        if workflow_id and await flow_closed(workflow_id, client):
            raise SessionBusy(FLOW_CLOSED)


async def is_open(flow_id: str, client: Any = None) -> bool:
    description = await _describe(flow_id, client)
    return bool(description and description.status == WorkflowExecutionStatus.RUNNING)
