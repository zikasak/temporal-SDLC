"""Every workflow execution in the namespace, newest first, from Temporal visibility."""

import base64
import binascii
import logging
import re
from datetime import datetime, timezone
from typing import Any

from temporalio.api.common.v1 import WorkflowExecution as ExecutionRef
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import (
    AddSearchAttributesRequest,
    ListSearchAttributesRequest,
)
from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest
from temporalio.client import Client, WorkflowExecution, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app.db import connect
from app.delivery import acceptances
from app.delivery.acceptances import RECORD_TYPE as ACCEPTANCE_RECORD_TYPE
from app.delivery.closed_mentions import WORKFLOW_TYPE as CLOSED_MENTION_TYPE
from app.delivery.models import FOLLOW_UP_PREFIX, delivery_id
from app.github.client import NAME_PATTERN as REPOSITORY_PATTERN
from app.task_flow.models import WORKFLOW_TYPE as TASK_WORKFLOW_TYPE
from app.task_flow.models import is_task_flow_id
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import (
    BRANCH_ATTRIBUTE,
    FLOW_STATE_ATTRIBUTE,
    ONBOARDING_ATTRIBUTE,
    PENDING,
    REPOSITORY_ATTRIBUTE,
    REPOSITORY_MEMO,
    SEARCH_ATTRIBUTES,
    SKIPPED,
    TASK_FLOW_MEMO,
    TASK_NAME_ATTRIBUTE,
    TASK_STAGES_MEMO,
    UNRESOLVED_ATTRIBUTE,
    WAITING_ATTRIBUTE,
)

logger = logging.getLogger(__name__)

WORKFLOW_TYPES = (
    "LLMRequestWorkflow",
    "TaskWorkflow",
    "RequirementsWorkflow",
    "ArchitectureWorkflow",
    "ApiDesignWorkflow",
    "OnboardingWorkflow",
    "DeliveryWorkflow",
    "CodegenWorkflow",
)
PAGE_SIZE = 50
ONBOARDING_ID_PATTERN = re.compile(r"onboarding-session-[0-9a-f]{32}")
MAX_BRANCH_LENGTH = 255
MAX_WORKFLOW_ID_LENGTH = 1000
# How many earlier stages of other tasks a task flow's listing of its children includes.
MAX_LINKED = 50
CLOSED_STATUSES = (WorkflowExecutionStatus.COMPLETED, WorkflowExecutionStatus.CANCELED,
                   WorkflowExecutionStatus.TERMINATED, WorkflowExecutionStatus.FAILED,
                   WorkflowExecutionStatus.TIMED_OUT)
# The one-time removal of executions started before they recorded these fields.
UNTRACKED_MIGRATION = "delete-executions-without-repository-fields"
_MIGRATION_LOCK = 0x5EA2C4A7


class InvalidPageToken(ValueError):
    pass


class InvalidFilter(ValueError):
    pass


def _quote(value: str) -> str:
    """A string literal of a visibility query."""
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def check_filters(repository: str | None, branch: str | None,
                  onboarding_id: str | None, parent: str | None = None,
                  top_level: bool = False) -> None:
    """Raise InvalidFilter for a malformed filter value; None means no filter."""
    if parent is not None:
        if top_level:
            raise InvalidFilter("parent and top_level cannot be combined.")
        if (not parent.strip() or len(parent) > MAX_WORKFLOW_ID_LENGTH
                or any(ord(c) < 32 or ord(c) == 127 for c in parent)):
            raise InvalidFilter("parent is not a valid workflow ID.")
    if repository is not None and not REPOSITORY_PATTERN.fullmatch(repository):
        raise InvalidFilter("repository must be of the form owner/name.")
    if branch is not None:
        if not branch.strip():
            raise InvalidFilter("branch must not be empty.")
        if len(branch) > MAX_BRANCH_LENGTH or any(ord(c) < 32 or ord(c) == 127 for c in branch):
            raise InvalidFilter("branch is not a valid branch name.")
    if onboarding_id is not None and not ONBOARDING_ID_PATTERN.fullmatch(onboarding_id):
        raise InvalidFilter(
            "onboarding_id must be onboarding-session- followed by 32 hex digits.")


def _query(workflow_type: str | None, repository: str | None, branch: str | None,
           onboarding_id: str | None, parent: str | None = None,
           top_level: bool = False, related: list[str] | None = None,
           legacy_parent: bool = True) -> str | None:
    # Records of acceptances of closed deliveries' findings are not flows, and neither
    # are the replies to mentions on the pull request of a delivery whose flow closed.
    clauses = [f"WorkflowType != '{ACCEPTANCE_RECORD_TYPE}'",
               f"WorkflowType != '{CLOSED_MENTION_TYPE}'"]
    # A session's delivery is its child, except for a run started again on its own after
    # the session closed: it is listed under the session by its ID, never at the top. A
    # flow that carries on a closed delivery, such as a fix a pull request comment asked
    # for after the pull request was merged, is a top-level flow of its own.
    if top_level:
        clauses.append("ParentWorkflowId IS NULL AND (WorkflowType != 'DeliveryWorkflow' "
                       f"OR WorkflowId STARTS_WITH '{delivery_id(FOLLOW_UP_PREFIX)}')")
    if parent is not None and related is not None:
        # A task flow's children, the earlier stages of another task it links to and its
        # delivery when that was started again on its own after the flow closed.
        ids = [_quote(item) for item in related[:MAX_LINKED]]
        clauses.append(f"(ParentWorkflowId = {_quote(parent)}"
                       + (f" OR WorkflowId IN ({', '.join(ids)})" if ids else "") + ")")
    elif parent is not None and legacy_parent:
        # A session from before task flows existed, or an onboarding: its delivery,
        # also a run started again on its own after the session closed.
        clauses.append(f"(ParentWorkflowId = {_quote(parent)} "
                       f"OR WorkflowId = {_quote(delivery_id(parent))})")
    elif parent is not None:
        # A stage of a task flow lists only its own children, never another stage.
        clauses.append(f"ParentWorkflowId = {_quote(parent)}")
    # workflow_type is checked against WORKFLOW_TYPES, so it is safe to inline.
    if workflow_type:
        clauses.append(f"WorkflowType = '{workflow_type}'")
    if repository is not None:
        clauses.append(f"{REPOSITORY_ATTRIBUTE.name} = {_quote(repository.lower())}")
    if branch is not None:
        clauses.append(f"{BRANCH_ATTRIBUTE.name} = {_quote(branch)}")
    if onboarding_id is not None:
        clauses.append(f"{ONBOARDING_ATTRIBUTE.name} = {_quote(onboarding_id)}")
    return " AND ".join(clauses) or None


async def _repository(item: WorkflowExecution) -> dict[str, Any] | None:
    try:
        selection = await item.memo_value(REPOSITORY_MEMO, None)
    except Exception:  # noqa: BLE001 - an unreadable memo lists as no repository
        logger.warning("Unreadable repository of execution %s", item.id, exc_info=True)
        return None
    if not isinstance(selection, dict) or not selection.get("full_name"):
        return None
    return {
        "provider": selection.get("provider", "github"),
        "repository_id": selection.get("repository_id"),
        "full_name": selection["full_name"],
        "html_url": selection.get("html_url"),
        "private": selection.get("private"),
        "branch": selection.get("branch"),
    }


async def _memo(item: Any, key: str) -> Any:
    try:
        return await item.memo_value(key, None)
    except Exception:  # noqa: BLE001 - an unreadable memo lists as none
        logger.warning("Unreadable %s of execution %s", key, item.id, exc_info=True)
        return None


async def _related(client: Client, task_flow_id: str) -> list[str]:
    """What a task flow lists besides its children: the earlier stages of another task
    it links to, and its deliveries, which may have been started again on their own."""
    try:
        description = await client.get_workflow_handle(task_flow_id).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return []
    stages = await _memo(description, TASK_STAGES_MEMO)
    if not isinstance(stages, dict):
        return []
    ids = [*(stages.get("links") or []), *(stages.get("delivery") or [])]
    return [item for item in dict.fromkeys(ids) if isinstance(item, str) and item]


async def _is_task_stage(client: Client, workflow_id: str) -> bool:
    """Whether `workflow_id` is a stage of a task flow: its child, or a run of one started
    again on its own, which keeps the flow's ID in its memo."""
    try:
        description = await client.get_workflow_handle(workflow_id).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        # A stage created by its task flow exists; an unknown ID is no stage.
        return False
    if description.parent_id and is_task_flow_id(description.parent_id):
        return True
    return isinstance(await _memo(description, TASK_FLOW_MEMO), str)


async def _delivery_run(client: Client, workflow_id: str) -> str | None:
    """The latest run of a delivery, which an acceptance of its findings is kept for."""
    try:
        return (await client.get_workflow_handle(workflow_id).describe()).run_id
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return None


async def _accepted(client: Client, item: WorkflowExecution) -> bool:
    """Whether the open findings of the closed flow's delivery were accepted after it
    closed, which its workflow could no longer record in its own search attributes."""
    try:
        if item.workflow_type == "DeliveryWorkflow":
            session_id, run_id = item.id.removeprefix("delivery-"), item.run_id
        elif item.workflow_type == "OnboardingWorkflow":
            session_id = item.id
            run_id = await _delivery_run(client, delivery_id(item.id))
        elif item.workflow_type == TASK_WORKFLOW_TYPE:
            stages = await _memo(item, TASK_STAGES_MEMO)
            deliveries = stages.get("delivery") if isinstance(stages, dict) else None
            if not deliveries:
                return False
            session_id = str(deliveries[-1]).removeprefix("delivery-")
            run_id = await _delivery_run(client, str(deliveries[-1]))
        else:
            return False
        if not run_id:
            return False
        return await acceptances.find(client, session_id, run_id) is not None
    except Exception:  # noqa: BLE001 - listed with the marker it carries
        logger.warning("Acceptance of the findings of %s not read", item.id, exc_info=True)
        return False


async def _execution(item: WorkflowExecution, client: Client | None = None,
                     parent: str | None = None) -> dict[str, Any]:
    attributes = item.typed_search_attributes
    repository = await _repository(item)
    branch = repository["branch"] if repository else None
    status = item.status.name.lower() if item.status else "unknown"
    # A stage of a task flow waiting for the stages it depends on, or one that will never
    # run, is listed as such rather than by its Temporal status.
    flow_state = attributes.get(FLOW_STATE_ATTRIBUTE)
    if flow_state in (PENDING, SKIPPED) and status != "continued_as_new":
        status = flow_state
    closed = item.status in CLOSED_STATUSES
    task_flow = await _memo(item, TASK_FLOW_MEMO)
    parent_id = item.parent_id or (task_flow if isinstance(task_flow, str) else None)
    waiting = attributes.get(WAITING_ATTRIBUTE)
    if waiting is not None and (closed or status in (PENDING, SKIPPED)):
        # A closed flow never waits for the user.
        waiting = False
    unresolved = attributes.get(UNRESOLVED_ATTRIBUTE)
    # A task flow still open, such as one waiting for linked stages of another task, may
    # carry the marker of its delivery that closed and whose findings were accepted since.
    if (unresolved and (closed or item.workflow_type == TASK_WORKFLOW_TYPE)
            and client is not None and await _accepted(client, item)):
        unresolved = False
    return {
        "workflow_id": item.id,
        "run_id": item.run_id,
        "workflow_type": item.workflow_type,
        "status": status,
        "task_queue": item.task_queue,
        "parent_workflow_id": parent_id,
        "start_time": item.start_time.isoformat(),
        "close_time": item.close_time.isoformat() if item.close_time else None,
        "repository": repository,
        "branch": branch or (attributes.get(BRANCH_ATTRIBUTE) if repository else None),
        "onboarding_id": attributes.get(ONBOARDING_ATTRIBUTE),
        "name": (attributes.get(TASK_NAME_ATTRIBUTE)
                 if item.workflow_type == TASK_WORKFLOW_TYPE else None),
        # Listed under a task flow as an earlier stage of another task it links to.
        "linked": bool(parent is not None and is_task_flow_id(parent)
                       and parent_id != parent),
        "waiting_for_user": waiting,
        "unresolved_findings": unresolved,
    }


async def list_executions(
    workflow_type: str | None = None, page_token: str | None = None, *,
    repository: str | None = None, branch: str | None = None,
    onboarding_id: str | None = None, parent: str | None = None, top_level: bool = False,
) -> dict[str, Any]:
    check_filters(repository, branch, onboarding_id, parent, top_level)
    try:
        token = base64.b64decode(page_token, altchars=b"-_", validate=True) if page_token else None
    except (binascii.Error, ValueError) as exc:
        raise InvalidPageToken("Invalid page token.") from exc
    client = await connect_temporal()
    related = (await _related(client, parent)
               if parent is not None and is_task_flow_id(parent) else None)
    legacy_parent = (parent is not None and related is None
                     and not await _is_task_stage(client, parent))
    query = _query(workflow_type, repository, branch, onboarding_id, parent, top_level,
                   related, legacy_parent)
    executions = client.list_workflows(query, page_size=PAGE_SIZE, next_page_token=token)
    await executions.fetch_next_page()
    next_token = executions.next_page_token
    return {
        "workflows": [await _execution(item, client, parent)
                      for item in executions.current_page or []],
        "next_page_token": base64.urlsafe_b64encode(next_token).decode() if next_token else None,
    }


async def ensure_search_attributes(client: Client) -> None:
    """Register the custom search attributes the executions are started with."""
    listed = await client.operator_service.list_search_attributes(
        ListSearchAttributesRequest(namespace=client.namespace))
    missing = {key.name: IndexedValueType.ValueType(key.indexed_value_type)
               for key in SEARCH_ATTRIBUTES if key.name not in listed.custom_attributes}
    if missing:
        await client.operator_service.add_search_attributes(
            AddSearchAttributesRequest(namespace=client.namespace, search_attributes=missing))
        logger.info("Registered search attributes %s", ", ".join(missing))


async def delete_untracked_executions(client: Client) -> None:
    """Once per instance: delete the executions started before executions recorded their
    repository, branch and onboarding ID, so that every listed execution carries them."""
    async with await connect() as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS app")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.temporal_migrations (
                name text PRIMARY KEY,
                done_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        await conn.commit()
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
            cursor = await conn.execute(
                "SELECT 1 FROM app.temporal_migrations WHERE name = %s", (UNTRACKED_MIGRATION,))
            if await cursor.fetchone() is not None:
                return
            cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            query = (f"StartTime < '{cutoff}' AND {REPOSITORY_ATTRIBUTE.name} IS NULL "
                     f"AND {ONBOARDING_ATTRIBUTE.name} IS NULL")
            deleted = 0
            async for item in client.list_workflows(query):
                await client.workflow_service.delete_workflow_execution(
                    DeleteWorkflowExecutionRequest(
                        namespace=client.namespace,
                        workflow_execution=ExecutionRef(workflow_id=item.id,
                                                        run_id=item.run_id)))
                deleted += 1
            await conn.execute(
                "INSERT INTO app.temporal_migrations (name) VALUES (%s)", (UNTRACKED_MIGRATION,))
            logger.info("Deleted %d executions without recorded repository fields", deleted)


async def prepare_namespace(client: Client) -> None:
    """Search attributes first, then the one-time cleanup; a failed cleanup is tried again
    at the next start and does not keep the worker from running."""
    await ensure_search_attributes(client)
    try:
        await delete_untracked_executions(client)
    except Exception:  # noqa: BLE001 - retried at the next start
        logger.exception("Could not delete executions without recorded repository fields")
