"""Starting task flows: one per new requirements interview, per fork of a requirements,
architecture or API design session, and per fix round on a closed task's delivery."""

import asyncio
import dataclasses
import hashlib
import logging
import uuid
from typing import Any

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.config import TASK_QUEUE
from app.tasks.models import (
    FIX_SUFFIX, FORK_SUFFIX, TASK_FLOW_TYPE, TaskFlowState, bounded_name, fallback_name,
    new_task_flow_id,
)
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import (
    TASK_NAME_ATTRIBUTE, execution_fields, task_fields,
)
from app.temporal.registry import register
from app.temporal.search_attributes import start_workflow

logger = logging.getLogger(__name__)

# How long a caller waits for a new task flow to have started its first stage.
STAGE_START_POLLS = 80
STAGE_START_POLL_SECONDS = 0.25

# The workflow type of each stage of a task flow, earlier ones first.
STAGE_TYPES = {
    "requirements": "RequirementsWorkflow",
    "architecture": "ArchitectureWorkflow",
    "api_design": "ApiDesignWorkflow",
    "delivery": "DeliveryWorkflow",
}


def new_ids() -> dict[str, str]:
    """IDs for a task flow and every stage it may create."""
    return {
        "task_flow_id": new_task_flow_id(uuid.uuid4().hex),
        "requirements_session_id": f"requirements-session-{uuid.uuid4().hex}",
        "architecture_session_id": f"architecture-session-{uuid.uuid4().hex}",
        "api_design_session_id": f"api-design-session-{uuid.uuid4().hex}",
    }


async def task_queue_of(workflow_id: str | None, client: Client | None = None) -> str:
    """The task queue the workflow runs on, so that a flow started from it, such as a
    fork or a fix round, runs where it ran; the default queue when it is unknown."""
    if not workflow_id:
        return TASK_QUEUE
    client = client or await connect_temporal()
    try:
        description = await client.get_workflow_handle(workflow_id).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return TASK_QUEUE
    return description.task_queue or TASK_QUEUE


async def start_task_flow(task_flow_id: str, state: TaskFlowState,
                          client: Client | None = None, task_queue: str | None = None,
                          first_stage: str | None = None) -> str | None:
    """Start the task flow; its first stage starts with it. With `first_stage`, wait until
    that stage exists, so that it can be served at once.

    When the workers of the queue cannot run task flows (a worker from before them), the
    task flow is ended and the first stage started on its own as a top-level flow, as it
    was then; None is returned for the task flow then."""
    client = client or await connect_temporal()
    task_queue = task_queue or TASK_QUEUE
    await start_workflow(
        client, TASK_FLOW_TYPE, state, id=task_flow_id, task_queue=task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        **execution_fields(state.repository,
                           task_name=bounded_name(state.name) if state.name else None),
    )
    await register(task_flow_id)
    logger.info("Started task flow %s (%s)", task_flow_id, state.origin)
    if first_stage:
        await register(first_stage)
        outcome = await _first_stage_started(task_flow_id, first_stage, client)
        if outcome == "unhosted":
            await _start_stage_alone(task_flow_id, state, first_stage, client, task_queue)
            return None
        if outcome != "started":
            logger.warning("Task flow %s has not started %s yet", task_flow_id, first_stage)
    return task_flow_id


async def _task_flow_unhosted(task_flow_id: str, client: Client) -> bool:
    """Whether the task flow's first workflow task failed before it ever completed, as
    when the worker that took it has no TaskWorkflow."""
    try:
        history = await client.get_workflow_handle(task_flow_id).fetch_history()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return False
    failed = False
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED:
            return False
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED:
            failed = True
    return failed


async def _first_stage_started(task_flow_id: str, stage_id: str, client: Client) -> str:
    """Wait a little for the task flow to create its first stage: 'started' once it
    exists, 'unhosted' when the task flow cannot run, 'waiting' when it did not yet."""
    for _ in range(STAGE_START_POLLS):
        try:
            await client.get_workflow_handle(stage_id).describe()
            return "started"
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
        if await _task_flow_unhosted(task_flow_id, client):
            return "unhosted"
        await asyncio.sleep(STAGE_START_POLL_SECONDS)
    return "waiting"


async def _start_stage_alone(task_flow_id: str, state: TaskFlowState, stage_id: str,
                             client: Client, task_queue: str) -> None:
    """End the task flow no worker runs and start its first stage as a top-level flow."""
    try:
        await client.get_workflow_handle(task_flow_id).terminate(
            reason="No worker runs task flows; the stage runs on its own.")
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
    for stage, workflow_type in STAGE_TYPES.items():
        stage_state = getattr(state, stage)
        if stage_state is not None:
            break
    else:
        return
    if stage == "delivery":
        fields = task_fields(stage_state.spec)
    else:
        if hasattr(stage_state, "task_flow_id"):
            stage_state.task_flow_id = None
        design = getattr(stage_state, "design", None)
        if design is not None and hasattr(design, "task_flow_id"):
            stage_state.design = dataclasses.replace(design, task_flow_id=None)
        fields = execution_fields(state.repository)
    try:
        await start_workflow(
            client, workflow_type, stage_state, id=stage_id, task_queue=task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL, **fields)
    except WorkflowAlreadyStartedError:
        return
    logger.warning("No worker runs task flow %s; started its %s %s on its own",
                   task_flow_id, stage, stage_id)


async def task_flow_state(task_flow_id: str | None,
                          client: Client | None = None) -> TaskFlowState | None:
    """The task flow's state, or None when there is none."""
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


async def task_name(task_flow_id: str | None, client: Client | None = None) -> str | None:
    """The task flow's current name, as listings show it."""
    if not task_flow_id:
        return None
    client = client or await connect_temporal()
    try:
        description = await client.get_workflow_handle(task_flow_id).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return None
    return description.typed_search_attributes.get(TASK_NAME_ATTRIBUTE)


async def derived_name(task_flow_id: str | None, fallback: str, suffix: str,
                       client: Client | None = None) -> str:
    """The name of a task flow started from another: the original's with `suffix`."""
    name = await task_name(task_flow_id, client) or fallback_name(fallback)
    for known in (FORK_SUFFIX, FIX_SUFFIX):
        while name.endswith(known):
            name = name[: -len(known)]
    return bounded_name(name, suffix)


def fork_name_source(requirements: list[Any], default: str) -> str:
    """What a fork of a session started before task flows is named after."""
    for item in requirements or []:
        title = item.get("title") if isinstance(item, dict) else None
        if title:
            return str(title)
    return default


async def wait_for_stage(workflow_id: str, client: Client | None = None) -> bool:
    """Wait a little for the task flow's stage `workflow_id` to exist."""
    client = client or await connect_temporal()
    for _ in range(STAGE_START_POLLS):
        try:
            await client.get_workflow_handle(workflow_id).describe()
            return True
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
        await asyncio.sleep(STAGE_START_POLL_SECONDS)
    return False


def fix_task_flow_id(follow_up_session_id: str) -> str:
    """The task flow of a fix round, derived from its session so that a handover tried
    again finds the flow it started."""
    digest = hashlib.sha256(f"{follow_up_session_id}:task-flow".encode()).hexdigest()[:32]
    return new_task_flow_id(digest)


async def start_fix_flow(client: Client, follow_up_session_id: str, delivery: Any,
                         original_task_flow_id: str | None, linked: list[str],
                         fallback: str, task_queue: str | None = None,
                         delivery_workflow_id: str | None = None) -> bool:
    """Start the new task flow of a fix round on a closed task's delivery on
    `task_queue`: it holds the fix delivery (`delivery`, named after
    `follow_up_session_id`, whose workflow is `delivery_workflow_id`) as its child and
    links to the original task's stages. False when it had been started already."""
    spec = delivery.spec
    state = TaskFlowState(
        kind=spec.kind, repository=spec.repository, origin="fix",
        name=await derived_name(original_task_flow_id, fallback, FIX_SUFFIX, client),
        delivery_session_id=follow_up_session_id, delivery=delivery,
        linked=[item for item in linked if item])
    task_flow_id = fix_task_flow_id(follow_up_session_id)
    try:
        await start_task_flow(task_flow_id, state, client, task_queue, delivery_workflow_id)
    except WorkflowAlreadyStartedError:
        return False
    return True


def stage_ids(state: TaskFlowState | None) -> list[str]:
    """The task flow's stages, earlier ones first, as a fork or fix round links them."""
    if state is None:
        return []
    order = ("requirements", "architecture", "api_design", "delivery")
    return [state.created[stage] for stage in order if state.created.get(stage)]
