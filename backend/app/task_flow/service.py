"""Task flows: one top-level TaskWorkflow per task, started for every new requirements
interview, for every fork of a requirements, architecture or API design session, and for
every fix round on a closed task's delivery."""

import hashlib
import logging
import uuid
from typing import Any

from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.api_design.models import ApiDesignState
from app.architecture.models import ArchitectureState
from app.config import TASK_QUEUE
from app.delivery.models import DeliveryState, delivery_id
from app.requirements.models import RequirementsState
from app.task_flow.models import (
    FIX_SUFFIX, FORK_SUFFIX, TASK_FLOW_PREFIX, WORKFLOW_TYPE, TaskFlowState,
)
from app.temporal import sessions
from app.temporal.execution_fields import execution_fields, task_name
from app.temporal.registry import register
from app.temporal.sessions import FLOW_CLOSED, SessionBusy, flow_closed

logger = logging.getLogger(__name__)


async def connect_temporal():
    """The Temporal client the session helpers reach, so that task flows run on the same
    server as the sessions and deliveries they hold."""
    return await sessions.connect_temporal()


async def _queue_of(workflow_id: str | None) -> str:
    """The task queue the workflow runs on, which a flow carrying it on keeps; the
    service's own when it cannot be read."""
    if not workflow_id:
        return TASK_QUEUE
    try:
        client = await connect_temporal()
        description = await client.get_workflow_handle(workflow_id).describe()
    except RPCError:
        return TASK_QUEUE
    return description.task_queue or TASK_QUEUE


def _fork_source(state: Any) -> str | None:
    origin = getattr(state, "forked_from", None)
    return origin.get("session_id") if isinstance(origin, dict) else None

# The name of a task flow whose task said nothing to name it from.
UNNAMED = "Untitled task"


def new_task_flow_id() -> str:
    return f"{TASK_FLOW_PREFIX}{uuid.uuid4().hex}"


def fix_task_flow_id(follow_up_session_id: str) -> str:
    """The task flow of the fix round `follow_up_session_id`: derived from it, so that a
    handover tried again finds the flow it started."""
    digest = hashlib.sha256(f"{follow_up_session_id}:task-flow".encode()).hexdigest()[:32]
    return f"{TASK_FLOW_PREFIX}{digest}"


def _suffixed(name: str | None, suffix: str) -> str:
    base = task_name(name or UNNAMED)
    if base.endswith(suffix):
        return base
    return task_name(base[:max(0, 120 - len(suffix))]) + suffix


async def get_task_flow(task_flow_id: str) -> TaskFlowState | None:
    """The task flow's state, None when it cannot be read, such as one that is gone."""
    client = await connect_temporal()
    try:
        return await client.get_workflow_handle(task_flow_id).query(
            "get_state", result_type=TaskFlowState)
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return None
    except Exception:  # noqa: BLE001 - an unreadable flow is treated as unknown
        logger.warning("Task flow %s could not be read", task_flow_id, exc_info=True)
        return None


def _unknown_search_attribute(exc: RPCError) -> bool:
    """Whether the server refused a start for naming a search attribute it does not know."""
    return (exc.status == RPCStatusCode.INVALID_ARGUMENT
            and "search attribute" in str(exc.message or exc).lower())


async def _start(state: TaskFlowState, task_flow_id: str, *,
                 existing_ok: bool = False, task_queue: str | None = None) -> str:
    """Start the task flow and wait until it has created its children, so that its first
    stage can be served at once."""
    client = await connect_temporal()

    async def start():
        return await client.start_workflow(
            WORKFLOW_TYPE, state, id=task_flow_id, task_queue=task_queue or TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            **execution_fields(state.repository, name=state.name, linked=state.linked))

    try:
        try:
            handle = await start()
        except RPCError as exc:
            if not _unknown_search_attribute(exc):
                raise
            # A namespace the workers have not prepared yet, such as a fresh server:
            # register the attributes the flow is started with and try once more.
            from app.temporal.executions import ensure_search_attributes
            await ensure_search_attributes(client)
            handle = await start()
    except WorkflowAlreadyStartedError:
        if not existing_ok:
            raise
        handle = client.get_workflow_handle(task_flow_id)
    await register(task_flow_id)
    await handle.execute_update("children_started")
    logger.info("Task flow %s started with its %s %s", task_flow_id, state.first_stage,
                state.first_id)
    return task_flow_id


async def start_task(state: RequirementsState, task: str) -> tuple[str, str]:
    """A new task flow for a new requirements interview from `state`: its session ID and
    the task flow's. The flow is named from `task` until the LLM has named it."""
    session_id = f"requirements-session-{uuid.uuid4().hex}"
    task_flow_id = new_task_flow_id()
    # Registered first, so that the session is known while the flow starts it.
    await register(session_id)
    await _start(TaskFlowState(
        kind=state.kind, repository=state.repository, task=task,
        name=task_name(task) or UNNAMED, first_stage="requirements", requirements=state,
        first_id=session_id, requirements_session_id=session_id,
    ), task_flow_id)
    return session_id, task_flow_id


async def _original(task_flow_id: str | None) -> TaskFlowState | None:
    """The task flow of the session a fork is made of; SessionBusy once it has closed."""
    if not task_flow_id:
        return None
    if await flow_closed(task_flow_id):
        raise SessionBusy(FLOW_CLOSED)
    return await get_task_flow(task_flow_id)


async def fork_requirements(source: RequirementsState, state: RequirementsState,
                            fallback_name: str) -> tuple[str, str]:
    """A new task flow holding the fork `state` of the requirements session `source`,
    with pending children for the stages after requirements."""
    original = await _original(source.task_flow_id)
    session_id = f"requirements-session-{uuid.uuid4().hex}"
    task_flow_id = new_task_flow_id()
    await register(session_id)
    await _start(TaskFlowState(
        kind=state.kind, repository=state.repository,
        task=original.task if original else fallback_name,
        name=_suffixed(original.name if original else fallback_name, FORK_SUFFIX),
        origin="fork", source_task_flow_id=source.task_flow_id,
        first_stage="requirements", requirements=state, first_id=session_id,
        requirements_session_id=session_id,
    ), task_flow_id, task_queue=await _queue_of(_fork_source(state)))
    return session_id, task_flow_id


async def fork_architecture(source: ArchitectureState, state: ArchitectureState) -> tuple[str, str]:
    """A new task flow holding the fork `state` of an architecture session, linked to the
    original task's requirements session, with pending API design and delivery."""
    original = await _original(source.task_flow_id)
    session_id = f"architecture-session-{uuid.uuid4().hex}"
    task_flow_id = new_task_flow_id()
    await register(session_id)
    linked = [source.source_session_id] if source.source_session_id else []
    await _start(TaskFlowState(
        kind=state.kind, repository=state.repository,
        task=original.task if original else "",
        name=_suffixed(original.name if original else None, FORK_SUFFIX),
        origin="fork", source_task_flow_id=source.task_flow_id,
        first_stage="architecture", architecture=state, first_id=session_id,
        requirements_session_id=source.source_session_id or None, linked=linked,
    ), task_flow_id, task_queue=await _queue_of(_fork_source(state)))
    return session_id, task_flow_id


async def fork_design(source: ApiDesignState, state: ApiDesignState) -> tuple[str, str]:
    """A new task flow holding the fork `state` of an API design, linked to the original
    task's requirements and architecture sessions, with a pending delivery."""
    original = await _original(source.task_flow_id)
    session_id = f"api-design-session-{uuid.uuid4().hex}"
    task_flow_id = new_task_flow_id()
    await register(session_id)
    linked = [item for item in (source.source_session_id, source.architecture_session_id)
              if item]
    await _start(TaskFlowState(
        kind=state.kind, repository=state.repository,
        task=original.task if original else "",
        name=_suffixed(original.name if original else None, FORK_SUFFIX),
        origin="fork", source_task_flow_id=source.task_flow_id,
        first_stage="api_design", design=state, first_id=session_id,
        requirements_session_id=source.source_session_id or None, linked=linked,
    ), task_flow_id, task_queue=await _queue_of(_fork_source(state)))
    return session_id, task_flow_id


def _stages_of(original: TaskFlowState | None, closed_delivery: str) -> list[str]:
    """The original task's stages a fix round's task flow links to."""
    stages = [child.workflow_id for child in (original.children if original else [])
              if child.status != "skipped"]
    if closed_delivery not in stages:
        stages.append(closed_delivery)
    return stages


async def start_fix_flow(original_task_flow_id: str, closed_session_id: str,
                         follow_up_session_id: str, state: DeliveryState) -> tuple[str, bool]:
    """The task flow of a fix round on the closed delivery of `closed_session_id`: a new
    top-level flow holding the fix delivery `state` as its child, linked to the original
    task's stages and named after it with a " (fix)" suffix. Its ID, and whether this
    call started it; one started before, such as by an earlier handover, is waited for
    to have started its delivery."""
    task_flow_id = fix_task_flow_id(follow_up_session_id)
    original = await get_task_flow(original_task_flow_id)
    flow = TaskFlowState(
        kind=state.spec.kind, repository=dict(state.spec.repository or {}) or None,
        task=original.task if original else "",
        name=_suffixed(original.name if original else None, FIX_SUFFIX),
        origin="fix", source_task_flow_id=original_task_flow_id,
        first_stage="delivery", delivery=state,
        first_id=delivery_id(follow_up_session_id),
        requirements_session_id=original.requirements_session_id if original else None,
        linked=_stages_of(original, delivery_id(closed_session_id)),
    )
    try:
        await _start(flow, task_flow_id,
                     task_queue=await _queue_of(delivery_id(closed_session_id)))
    except WorkflowAlreadyStartedError:
        client = await connect_temporal()
        await client.get_workflow_handle(task_flow_id).execute_update("children_started")
        return task_flow_id, False
    return task_flow_id, True


def flow_of(spec: Any) -> str | None:
    """The task flow a delivery's task belongs to, if any."""
    return getattr(spec, "task_flow_id", None)
