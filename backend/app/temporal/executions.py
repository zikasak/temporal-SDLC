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
from app.delivery.models import FOLLOW_UP_PREFIX, delivery_id
from app.github.client import NAME_PATTERN as REPOSITORY_PATTERN
from app.tasks.models import TASK_FLOW_PREFIX
from app.tasks.models import WORKFLOW_TYPE as TASK_WORKFLOW_TYPE
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import (
    BRANCH_ATTRIBUTE,
    DELIVERY_MEMO,
    FLOW_STATE_ATTRIBUTE,
    LINKED_MEMO,
    ONBOARDING_ATTRIBUTE,
    PENDING,
    REPOSITORY_ATTRIBUTE,
    REPOSITORY_MEMO,
    SEARCH_ATTRIBUTES,
    SKIPPED,
    STAGES_MEMO,
    TASK_NAME_ATTRIBUTE,
    UNRESOLVED_ATTRIBUTE,
    WAITING_ATTRIBUTE,
)

logger = logging.getLogger(__name__)

WORKFLOW_TYPES = (
    "LLMRequestWorkflow",
    TASK_WORKFLOW_TYPE,
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


def _parent_clause(parent: str) -> str:
    """The children of a flow started before task flows existed: its own, and its
    session's delivery started again on its own after the session closed."""
    return (f"(ParentWorkflowId = {_quote(parent)} "
            f"OR WorkflowId = {_quote(delivery_id(parent))})")


def _query(workflow_type: str | None, repository: str | None, branch: str | None,
           onboarding_id: str | None, parent: str | None = None,
           top_level: bool = False, parent_clause: str | None = None) -> str | None:
    # Records of acceptances of closed deliveries' findings are not flows.
    clauses = [f"WorkflowType != '{ACCEPTANCE_RECORD_TYPE}'"]
    # A session's delivery is its child, except for a run started again on its own after
    # the session closed: it is listed under the session by its ID, never at the top. A
    # flow that carries on a closed delivery, such as a fix a pull request comment asked
    # for after the pull request was merged, is a top-level flow of its own. A stage of a
    # task flow is always its task flow's child.
    if top_level:
        clauses.append("ParentWorkflowId IS NULL AND (WorkflowType != 'DeliveryWorkflow' "
                       f"OR WorkflowId STARTS_WITH '{delivery_id(FOLLOW_UP_PREFIX)}')")
    if parent is not None:
        clauses.append(parent_clause or _parent_clause(parent))
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


def _status(item: WorkflowExecution) -> str:
    """The Temporal status in lower case, or a task flow stage's pending or skipped."""
    status = item.status.name.lower() if item.status else "unknown"
    attributes = item.typed_search_attributes
    flow_state = attributes.get(FLOW_STATE_ATTRIBUTE) if attributes else None
    if flow_state == PENDING and item.status == WorkflowExecutionStatus.RUNNING:
        return PENDING
    if flow_state == SKIPPED and item.status == WorkflowExecutionStatus.COMPLETED:
        return SKIPPED
    return status


def _flag(item: WorkflowExecution, key: Any) -> bool | None:
    attributes = item.typed_search_attributes
    value = attributes.get(key) if attributes else None
    return bool(value) if value is not None else None


async def _execution(item: WorkflowExecution, linked: set[str] = frozenset(),
                     parent: str | None = None) -> dict[str, Any]:
    attributes = item.typed_search_attributes
    repository = await _repository(item)
    branch = repository["branch"] if repository else None
    waiting = _flag(item, WAITING_ATTRIBUTE)
    if waiting is not None and item.status != WorkflowExecutionStatus.RUNNING:
        # A closed flow waits for nothing.
        waiting = False
    return {
        "workflow_id": item.id,
        "run_id": item.run_id,
        "workflow_type": item.workflow_type,
        "status": _status(item),
        "task_queue": item.task_queue,
        "parent_workflow_id": item.parent_id,
        "start_time": item.start_time.isoformat(),
        "close_time": item.close_time.isoformat() if item.close_time else None,
        "repository": repository,
        "branch": branch or (attributes.get(BRANCH_ATTRIBUTE) if repository else None),
        "onboarding_id": attributes.get(ONBOARDING_ATTRIBUTE),
        "name": (attributes.get(TASK_NAME_ATTRIBUTE)
                 if item.workflow_type == TASK_WORKFLOW_TYPE else None),
        "linked": bool(parent is not None and item.id in linked and item.parent_id != parent),
        "waiting_for_user": waiting,
        "unresolved_findings": _flag(item, UNRESOLVED_ATTRIBUTE),
    }


async def _memo_list(item: Any, key: str) -> list[str]:
    try:
        value = await item.memo_value(key, None)
    except Exception:  # noqa: BLE001 - an unreadable memo lists as none
        logger.warning("Unreadable %s of execution %s", key, item.id, exc_info=True)
        return []
    if isinstance(value, str):
        return [value]
    return [entry for entry in value or [] if isinstance(entry, str)]


async def _parent_scope(client: Client, parent: str) -> tuple[str | None, set[str]]:
    """The clause listing the children of `parent`, and the IDs among them of earlier
    stages of another task that a task flow links to. A task flow lists its stages (a
    delivery started again on its own included) and the stages it links to; a stage of
    a task lists only its own children, never another stage of the task; any other flow
    lists its children as before."""
    try:
        description = await client.get_workflow_handle(parent).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return None, set()
    if description.workflow_type == TASK_WORKFLOW_TYPE:
        stages = await _memo_list(description, STAGES_MEMO)
        linked = await _memo_list(description, LINKED_MEMO)
        ids = list(dict.fromkeys([*stages, *linked]))
        clause = " OR ".join([f"ParentWorkflowId = {_quote(parent)}",
                              *(f"WorkflowId = {_quote(item)}" for item in ids)])
        return f"({clause})", set(linked)
    if (description.parent_id or "").startswith(TASK_FLOW_PREFIX):
        return f"ParentWorkflowId = {_quote(parent)}", set()
    return None, set()


def _any_id(ids: list[str], column: str = "WorkflowId") -> str:
    return "(" + " OR ".join(f"{column} = {_quote(item)}" for item in ids) + ")"


async def _accepted(client: Client, delivery_workflow_id: str, run_id: str) -> bool:
    """Whether the open findings of the closed delivery run were accepted after it
    closed, which its workflow could no longer record."""
    session_id = delivery_workflow_id.removeprefix("delivery-")
    return await acceptances.find(client, session_id, run_id) is not None


async def _decorate(client: Client, items: list[WorkflowExecution],
                    records: list[dict[str, Any]]) -> None:
    """Fill in what the listed flows' own attributes cannot say: whether a task flow or
    an onboarding waits for the user through any of its children, whether its delivery
    has unresolved findings, and for closed deliveries whether their findings were
    accepted after they closed."""
    owners: dict[str, str] = {}   # owner workflow ID -> its delivery's workflow ID
    for item, record in zip(items, records):
        if item.workflow_type == TASK_WORKFLOW_TYPE:
            delivery = await _memo_list(item, DELIVERY_MEMO)
            owners[item.id] = delivery[0] if delivery else ""
            record["waiting_for_user"] = False
            record["unresolved_findings"] = False
        elif item.workflow_type == "OnboardingWorkflow" and record["waiting_for_user"] is not None:
            owners[item.id] = delivery_id(item.id)
            record["unresolved_findings"] = False
        elif (item.workflow_type == "DeliveryWorkflow" and record["unresolved_findings"]
              and item.status in (WorkflowExecutionStatus.COMPLETED,
                                  WorkflowExecutionStatus.CANCELED)):
            if await _accepted(client, item.id, item.run_id):
                record["unresolved_findings"] = False
    if not owners:
        return
    running = [item.id for item in items
               if item.id in owners and item.status == WorkflowExecutionStatus.RUNNING]
    deliveries = sorted({delivery for delivery in owners.values() if delivery})
    waiting_owners: set[str] = set()
    if running:
        # A child that waits, or a task flow's delivery started again on its own.
        scope = " OR ".join([_any_id(running, "ParentWorkflowId"),
                             *([_any_id(deliveries)] if deliveries else [])])
        query = (f"ExecutionStatus = 'Running' AND {WAITING_ATTRIBUTE.name} = true "
                 f"AND ({scope})")
        owner_of = {delivery: owner for owner, delivery in owners.items() if delivery}
        async for child in client.list_workflows(query, limit=1000):
            waiting_owners.add(child.parent_id if child.parent_id in owners
                               else owner_of.get(child.id, ""))
    latest: dict[str, WorkflowExecution] = {}
    if deliveries:
        async for child in client.list_workflows(
                f"WorkflowType = 'DeliveryWorkflow' AND {_any_id(deliveries)}", limit=1000):
            # Newest first: the first run of each is its latest.
            latest.setdefault(child.id, child)
    for item, record in zip(items, records):
        if item.id not in owners:
            continue
        if item.status == WorkflowExecutionStatus.RUNNING:
            record["waiting_for_user"] = (bool(record["waiting_for_user"])
                                          or item.id in waiting_owners)
        else:
            record["waiting_for_user"] = False
        delivery = latest.get(owners[item.id])
        unresolved = bool(delivery is not None and _flag(delivery, UNRESOLVED_ATTRIBUTE))
        if unresolved and delivery.status in (WorkflowExecutionStatus.COMPLETED,
                                              WorkflowExecutionStatus.CANCELED):
            unresolved = not await _accepted(client, delivery.id, delivery.run_id)
        record["unresolved_findings"] = unresolved


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
    parent_clause, linked = (await _parent_scope(client, parent) if parent is not None
                             else (None, set()))
    query = _query(workflow_type, repository, branch, onboarding_id, parent, top_level,
                   parent_clause)
    executions = client.list_workflows(query, page_size=PAGE_SIZE, next_page_token=token)
    await executions.fetch_next_page()
    next_token = executions.next_page_token
    items = list(executions.current_page or [])
    records = [await _execution(item, linked, parent) for item in items]
    await _decorate(client, items, records)
    return {
        "workflows": records,
        "next_page_token": base64.urlsafe_b64encode(next_token).decode() if next_token else None,
    }


async def ensure_search_attributes(client: Client) -> None:
    """Register the custom search attributes the executions are started with."""
    listed = await client.operator_service.list_search_attributes(
        ListSearchAttributesRequest(namespace=client.namespace))
    # The SDK's indexed value types are numbered as the API's.
    missing = {key.name: IndexedValueType.ValueType(int(key.indexed_value_type))
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
