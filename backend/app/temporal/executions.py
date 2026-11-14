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
from temporalio.client import Client, WorkflowExecution

from temporalio.client import WorkflowExecutionStatus
from temporalio.common import SearchAttributeKey
from temporalio.service import RPCError, RPCStatusCode

from app.db import connect
from app.delivery import acceptances
from app.delivery.acceptances import RECORD_TYPE as ACCEPTANCE_RECORD_TYPE
from app.delivery.models import FOLLOW_UP_PREFIX, delivery_id
from app.github.client import NAME_PATTERN as REPOSITORY_PATTERN
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import (
    BRANCH_ATTRIBUTE,
    FLOW_STATE_ATTRIBUTE,
    LINKED_ATTRIBUTE,
    LINKED_MEMO,
    ONBOARDING_ATTRIBUTE,
    PENDING,
    REPOSITORY_ATTRIBUTE,
    REPOSITORY_MEMO,
    SEARCH_ATTRIBUTES,
    SKIPPED,
    TASK_FLOW_ATTRIBUTE,
    TASK_NAME_ATTRIBUTE,
    UNRESOLVED_ATTRIBUTE,
    WAITING_ATTRIBUTE,
)

logger = logging.getLogger(__name__)

WORKFLOW_TYPES = (
    "TaskWorkflow",
    "LLMRequestWorkflow",
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
# How many earlier stages of another task a task flow's listing links to at most.
MAX_LINKED = 20
CLOSED_STATUSES = (WorkflowExecutionStatus.COMPLETED, WorkflowExecutionStatus.CANCELED,
                   WorkflowExecutionStatus.FAILED, WorkflowExecutionStatus.TERMINATED,
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
           top_level: bool = False, linked: list[str] | None = None) -> str | None:
    # Records of acceptances of closed deliveries' findings are not flows.
    clauses = [f"WorkflowType != '{ACCEPTANCE_RECORD_TYPE}'"]
    # A stage of a task flow is never top-level, even a delivery started again on its
    # own. Before task flows, a session's delivery was its child, except for a run started
    # again on its own after the session closed: it is listed under the session by its ID,
    # never at the top. A flow that carries on a closed delivery without a task flow, such
    # as an onboarding's fix a pull request comment asked for after the pull request was
    # merged, is a top-level flow of its own.
    if top_level:
        clauses.append(f"ParentWorkflowId IS NULL AND {TASK_FLOW_ATTRIBUTE.name} IS NULL "
                       "AND (WorkflowType != 'DeliveryWorkflow' "
                       f"OR WorkflowId STARTS_WITH '{delivery_id(FOLLOW_UP_PREFIX)}')")
    if parent is not None:
        # A task flow's stages, a delivery of it started again on its own included, and the
        # earlier stages of another task a fork or fix round links to; a session's own
        # delivery started again on its own only for sessions without a task flow.
        options = [f"ParentWorkflowId = {_quote(parent)}",
                   f"{TASK_FLOW_ATTRIBUTE.name} = {_quote(parent)}",
                   f"{LINKED_ATTRIBUTE.name} = {_quote(parent)}",
                   f"(WorkflowId = {_quote(delivery_id(parent))} "
                   f"AND {TASK_FLOW_ATTRIBUTE.name} IS NULL)"]
        if linked:
            options.append("WorkflowId IN (" + ", ".join(_quote(item) for item in linked) + ")")
        clauses.append("(" + " OR ".join(options) + ")")
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


def _attribute(item: WorkflowExecution, key: SearchAttributeKey) -> Any:
    try:
        return item.typed_search_attributes.get(key)
    except Exception:  # noqa: BLE001 - an attribute of another type counts as absent
        return None


def _status(item: WorkflowExecution) -> str:
    """The Temporal status, or pending or skipped for a stage of a task flow that has not
    started yet or never will."""
    status = item.status.name.lower() if item.status else "unknown"
    state = _attribute(item, FLOW_STATE_ATTRIBUTE)
    if state == SKIPPED and item.status == WorkflowExecutionStatus.COMPLETED:
        return SKIPPED
    if state == PENDING and item.status == WorkflowExecutionStatus.RUNNING:
        return PENDING
    if state == SKIPPED and item.status == WorkflowExecutionStatus.RUNNING:
        # Closing as skipped.
        return SKIPPED
    return status


async def _accepted(client: Client, item: WorkflowExecution) -> bool:
    """Whether the open review findings of the closed flow `item` were accepted after it
    closed: the acceptance record of its delivery's closed run exists."""
    try:
        if item.workflow_type == "DeliveryWorkflow":
            session_id = item.id.removeprefix("delivery-")
            return await acceptances.find(client, session_id, item.run_id) is not None
        if item.workflow_type == "OnboardingWorkflow":
            description = await client.get_workflow_handle(delivery_id(item.id)).describe()
            return await acceptances.find(client, item.id, description.run_id) is not None
        if item.workflow_type == "TaskWorkflow":
            query = (f"{TASK_FLOW_ATTRIBUTE.name} = {_quote(item.id)} "
                     "AND WorkflowType = 'DeliveryWorkflow' "
                     f"AND {UNRESOLVED_ATTRIBUTE.name} = true")
            async for delivery in client.list_workflows(query, limit=5):
                session_id = delivery.id.removeprefix("delivery-")
                if await acceptances.find(client, session_id, delivery.run_id) is None:
                    return False
            return True
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            logger.warning("Acceptance of the findings of %s not read: %s", item.id, exc)
    return False


async def _execution(client: Client, item: WorkflowExecution,
                     parent: str | None = None) -> dict[str, Any]:
    attributes = item.typed_search_attributes
    repository = await _repository(item)
    branch = repository["branch"] if repository else None
    # A stage of a task flow, such as a delivery started again on its own, belongs to it.
    parent_id = item.parent_id or _attribute(item, TASK_FLOW_ATTRIBUTE)
    closed = item.status in CLOSED_STATUSES
    waiting = _attribute(item, WAITING_ATTRIBUTE)
    unresolved = _attribute(item, UNRESOLVED_ATTRIBUTE)
    if waiting is not None and closed:
        # A closed flow never waits for the user.
        waiting = False
    if unresolved and closed and await _accepted(client, item):
        unresolved = False
    return {
        "workflow_id": item.id,
        "run_id": item.run_id,
        "workflow_type": item.workflow_type,
        "status": _status(item),
        "task_queue": item.task_queue,
        "parent_workflow_id": parent_id,
        "start_time": item.start_time.isoformat(),
        "close_time": item.close_time.isoformat() if item.close_time else None,
        "repository": repository,
        "branch": branch or (attributes.get(BRANCH_ATTRIBUTE) if repository else None),
        "onboarding_id": attributes.get(ONBOARDING_ATTRIBUTE),
        "name": (_attribute(item, TASK_NAME_ATTRIBUTE)
                 if item.workflow_type == "TaskWorkflow" else None),
        # Listed under a fork's or fix round's task flow as an earlier stage it links to.
        "linked": parent is not None and parent_id != parent and item.id != delivery_id(parent),
        "waiting_for_user": None if waiting is None else bool(waiting),
        "unresolved_findings": None if unresolved is None else bool(unresolved),
    }


async def _linked(client: Client, parent: str) -> list[str]:
    """The earlier stages of another task the task flow `parent` links to."""
    try:
        description = await client.get_workflow_handle(parent).describe()
        linked = await description.memo_value(LINKED_MEMO, None)
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return []
    except Exception:  # noqa: BLE001 - an unreadable memo links to nothing
        logger.warning("Unreadable links of %s", parent, exc_info=True)
        return []
    if not isinstance(linked, list):
        return []
    return [str(item) for item in linked if isinstance(item, str) and item][:MAX_LINKED]


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
    await _ensure_once(client)
    linked = await _linked(client, parent) if parent is not None else []
    query = _query(workflow_type, repository, branch, onboarding_id, parent, top_level,
                   linked)
    executions = client.list_workflows(query, page_size=PAGE_SIZE, next_page_token=token)
    await executions.fetch_next_page()
    next_token = executions.next_page_token
    return {
        "workflows": [await _execution(client, item, parent)
                      for item in executions.current_page or []],
        "next_page_token": base64.urlsafe_b64encode(next_token).decode() if next_token else None,
    }


# The namespaces whose search attributes this process has made sure of.
_ENSURED: set[str] = set()


async def _ensure_once(client: Client) -> None:
    """Make sure, once per namespace, that the search attributes the listing queries
    exist, as the workers register them when they start: a query naming an unknown one
    fails. A failure is tried again with the next listing."""
    if client.namespace in _ENSURED:
        return
    try:
        await ensure_search_attributes(client)
    except RPCError as exc:
        logger.warning("Search attributes not registered: %s", exc)
        return
    _ENSURED.add(client.namespace)


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
