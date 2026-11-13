"""Task flows: one top-level TaskWorkflow per task, started for every new requirements
interview, every fork of a requirements, architecture or API design session and every
fix round on the delivery of a task whose flow closed."""

import asyncio
import logging
import uuid
from typing import Any

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import (
    SearchAttributePair, TypedSearchAttributes, WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.service import RPCError, RPCStatusCode

from app.api_design.models import ApiDesignState
from app.architecture.models import ArchitectureState
from app.config import TASK_QUEUE
from app.delivery.models import DeliveryState, delivery_id
from app.requirements.models import DIRECT_KINDS, RequirementsState
from app.task_flow.models import (
    DESIGNED_KINDS, FIX_SUFFIX, FORK_SUFFIX, WORKFLOW_TYPE, StageRecord, TaskState,
    fallback_name, new_task_flow_id, stage_ids, suffixed,
)
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import (
    TASK_NAME_ATTRIBUTE, TASK_STAGES_MEMO, execution_fields,
)
from app.temporal.registry import register

logger = logging.getLogger(__name__)

# How long a start waits for the stage the task flow starts at once to exist, so that
# the session it returns can be read right away.
STAGE_START_POLLS = 40
STAGE_START_POLL_SECONDS = 0.25


def _session_id(prefix: str) -> str:
    return f"{prefix}-session-{uuid.uuid4().hex}"


def later_stages(kind: str | None, requirements_id: str) -> list[StageRecord]:
    """The pending stages after the requirements of a task of `kind`."""
    if kind in DESIGNED_KINDS:
        design = _session_id("api-design")
        return [StageRecord("architecture", _session_id("architecture")),
                StageRecord("api_design", design),
                StageRecord("delivery", delivery_id(design))]
    if kind in DIRECT_KINDS:
        return [StageRecord("delivery", delivery_id(requirements_id))]
    return []


async def _client(client: Client | None) -> Client:
    return client if client is not None else await connect_temporal()


async def _start(task_flow_id: str, state: TaskState, first: str, client: Client | None = None,
                 task_queue: str | None = None) -> None:
    """Start the task flow on `task_queue`, by default the service's own, through
    `client` and wait for its first stage `first` to exist."""
    fields = execution_fields(state.repository, flags=True, findings=True)
    pairs = list(fields.get("search_attributes", TypedSearchAttributes([])).search_attributes)
    pairs.append(SearchAttributePair(TASK_NAME_ATTRIBUTE, state.name))
    fields["search_attributes"] = TypedSearchAttributes(pairs)
    fields.setdefault("memo", {})[TASK_STAGES_MEMO] = stage_ids(state)
    client = await _client(client)
    # One started already, such as by a concurrent handover of the same fix round,
    # raises WorkflowAlreadyStartedError: that one is the flow.
    await client.start_workflow(
        WORKFLOW_TYPE, state, id=task_flow_id, task_queue=task_queue or TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL, **fields)
    await register(task_flow_id)
    logger.info("Started task flow %s (%s) with %s", task_flow_id, state.origin, first)
    for _ in range(STAGE_START_POLLS):
        try:
            await client.get_workflow_handle(first).describe()
            return
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
        await asyncio.sleep(STAGE_START_POLL_SECONDS)


async def start_requirements(state: RequirementsState, task: str,
                             forked_from: str | None = None, *, client: Client | None = None,
                             task_queue: str | None = None) -> tuple[str, str]:
    """A new task flow holding the requirements session `state` as its first stage, for a
    new interview from the intake's `task`, or a fork of the requirements session of
    task flow `forked_from`. Returns the session's and the task flow's IDs."""
    task_flow_id = new_task_flow_id(uuid.uuid4().hex)
    session_id = _session_id("requirements")
    state.task_flow_id = task_flow_id
    flow = TaskState(kind=state.kind, repository=state.repository, task=task,
                     stages=[StageRecord("requirements", session_id, "running"),
                             *later_stages(state.kind, session_id)],
                     requirements=state)
    if forked_from is None:
        flow.name = fallback_name(task)
    else:
        # A fork of a session from before task flows existed has none: `forked_from` is
        # then empty.
        flow.origin = "fork"
        flow.source_task_flow_id = forked_from or None
        name = await _name_of(forked_from, client) or fallback_name(task)
        flow.name = suffixed(name, FORK_SUFFIX)
    await register(session_id)
    await _start(task_flow_id, flow, session_id, client, task_queue)
    return session_id, task_flow_id


async def start_architecture_fork(state: ArchitectureState,
                                  source_task_flow_id: str | None, *,
                                  client: Client | None = None,
                                  task_queue: str | None = None) -> tuple[str, str]:
    """A new task flow holding the forked architecture session `state`, linked to its
    task's requirements session, with pending API design and delivery stages."""
    task_flow_id = new_task_flow_id(uuid.uuid4().hex)
    session_id = _session_id("architecture")
    design = _session_id("api-design")
    state.task_flow_id = task_flow_id
    state.next_session_id = design
    state.pending = False
    flow = TaskState(kind=state.kind, repository=state.repository, origin="fork",
                     source_task_flow_id=source_task_flow_id,
                     stages=[StageRecord("architecture", session_id, "running"),
                             StageRecord("api_design", design),
                             StageRecord("delivery", delivery_id(design))],
                     links=[item for item in [state.source_session_id] if item],
                     architecture=state)
    flow.name = suffixed(await _name_of(source_task_flow_id, client) or "Task", FORK_SUFFIX)
    await register(session_id)
    await _start(task_flow_id, flow, session_id, client, task_queue)
    return session_id, task_flow_id


async def start_api_design_fork(state: ApiDesignState,
                                source_task_flow_id: str | None, *,
                                client: Client | None = None,
                                task_queue: str | None = None) -> tuple[str, str]:
    """A new task flow holding the forked API design `state`, linked to its task's
    requirements and architecture sessions, with a pending delivery stage."""
    task_flow_id = new_task_flow_id(uuid.uuid4().hex)
    session_id = _session_id("api-design")
    state.task_flow_id = task_flow_id
    state.pending = False
    links = [item for item in (state.source_session_id, state.architecture_session_id) if item]
    flow = TaskState(kind=state.kind, repository=state.repository, origin="fork",
                     source_task_flow_id=source_task_flow_id,
                     stages=[StageRecord("api_design", session_id, "running"),
                             StageRecord("delivery", delivery_id(session_id))],
                     links=links, api_design=state)
    flow.name = suffixed(await _name_of(source_task_flow_id, client) or "Task", FORK_SUFFIX)
    await register(session_id)
    await _start(task_flow_id, flow, session_id, client, task_queue)
    return session_id, task_flow_id


async def start_fix(task_flow_id: str, state: DeliveryState, source_task_flow_id: str | None,
                    stages: list[str]) -> None:
    """A new task flow `task_flow_id` holding the fix round's delivery `state` of the
    closed delivery of task flow `source_task_flow_id`, linked to that task's stages, or
    to `stages` for a task from before task flows existed."""
    state.task_flow_id = task_flow_id
    state.pending = False
    source = await get_state(source_task_flow_id) if source_task_flow_id else None
    links = ([record.workflow_id for record in source.stages if record.status != "skipped"]
             if source else [item for item in stages if item])
    flow = TaskState(kind=state.spec.kind, repository=state.spec.repository, origin="fix",
                     source_task_flow_id=source_task_flow_id,
                     stages=[StageRecord("delivery", delivery_id(state.spec.session_id),
                                         "running")],
                     links=links, delivery=state)
    flow.name = suffixed((source.name if source else "") or "Task", FIX_SUFFIX)
    await _start(task_flow_id, flow, delivery_id(state.spec.session_id))


async def get_state(task_flow_id: str, client: Client | None = None) -> TaskState | None:
    """The task flow's state; None when there is no such flow."""
    client = await _client(client)
    try:
        return await client.get_workflow_handle(task_flow_id).query(
            "get_state", result_type=TaskState)
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return None


async def _name_of(task_flow_id: str | None, client: Client | None = None) -> str:
    if not task_flow_id:
        return ""
    try:
        state = await get_state(task_flow_id, client)
    except Exception:  # noqa: BLE001 - a fork is still named, from its own suffix
        logger.warning("Name of task flow %s not read", task_flow_id, exc_info=True)
        return ""
    return state.name if state else ""


async def started_stage(task_flow_id: str | None, stage: str,
                        client: Client | None = None) -> str | None:
    """The ID of the task flow's `stage` once the flow started it; None while it is
    pending, when it was skipped or when the flow cannot be read."""
    if not task_flow_id:
        return None
    try:
        state = await get_state(task_flow_id, client)
    except Exception:  # noqa: BLE001 - the session is still served without it
        logger.warning("Task flow %s not read", task_flow_id, exc_info=True)
        return None
    if state is None:
        return None
    started = [record.workflow_id for record in state.stages
               if record.stage == stage and record.status in ("running", "stopped", "closed")]
    return started[-1] if started else None


async def is_closed(task_flow_id: str, client: Client | None = None) -> bool:
    """Whether the task flow completed or was cancelled."""
    client = await _client(client)
    try:
        description = await client.get_workflow_handle(task_flow_id).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return False
    return description.status in (WorkflowExecutionStatus.COMPLETED,
                                  WorkflowExecutionStatus.CANCELED)


def session_fields(state: Any) -> dict[str, Any]:
    """Internal fields of a stage's state that its session response leaves out."""
    return {key: value for key, value in (state or {}).items()
            if key not in ("pending", "next_session_id")}
