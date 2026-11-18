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
from temporalio.service import RPCError, RPCStatusCode

from app.db import connect
from app.delivery import acceptances
from app.delivery.acceptances import RECORD_TYPE as ACCEPTANCE_RECORD_TYPE
from app.delivery.models import FOLLOW_UP_PREFIX, delivery_id
from app.github.client import NAME_PATTERN as REPOSITORY_PATTERN
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import (
    BRANCH_ATTRIBUTE,
    DELIVERY_SESSION_MEMO,
    FLOW_STATE_ATTRIBUTE,
    LINKED_ATTRIBUTE,
    LINKED_STAGES_MEMO,
    ONBOARDING_ATTRIBUTE,
    REPOSITORY_ATTRIBUTE,
    REPOSITORY_MEMO,
    SEARCH_ATTRIBUTES,
    TASK_NAME_ATTRIBUTE,
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
    # A session's delivery is its child, except for a run started again on its own after
    # the session closed: it is listed under the session by its ID, never at the top. A
    # flow that carries on a closed delivery, such as a fix a pull request comment asked
    # for after the pull request was merged, is a top-level flow of its own, as is the
    # task flow that holds the fix round of a closed task's delivery. Stages of a task are
    # children of its task flow, so never top-level.
    if top_level:
        clauses.append("ParentWorkflowId IS NULL AND (WorkflowType != 'DeliveryWorkflow' "
                       f"OR WorkflowId STARTS_WITH '{delivery_id(FOLLOW_UP_PREFIX)}')")
    if parent is not None:
        # The direct children, executions started on their own but listed under the flow
        # (a task's delivery started again after its task flow closed), the earlier stages
        # of another task a forked or fix-round task flow links to and, for a session from
        # before task flows, its delivery started again on its own. A stage's own
        # delivery-like ID never lists another stage of its task: that has a parent.
        alternatives = [f"ParentWorkflowId = {_quote(parent)}",
                        f"{LINKED_ATTRIBUTE.name} = {_quote(parent)}",
                        f"(WorkflowId = {_quote(delivery_id(parent))} "
                        f"AND ParentWorkflowId IS NULL AND {LINKED_ATTRIBUTE.name} IS NULL)"]
        if linked:
            alternatives.append(
                "WorkflowId IN (" + ", ".join(_quote(item) for item in linked) + ")")
        clauses.append("(" + " OR ".join(alternatives) + ")")
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


# Executions that can wait for the user, and those that have a delivery.
WAITING_TYPES = ("TaskWorkflow", "RequirementsWorkflow", "ArchitectureWorkflow",
                 "ApiDesignWorkflow", "OnboardingWorkflow", "DeliveryWorkflow")
FINDINGS_TYPES = ("TaskWorkflow", "OnboardingWorkflow", "DeliveryWorkflow")
OPEN_STATUSES = ("running", "continued_as_new")


async def _memo(item: WorkflowExecution, key: str) -> Any:
    try:
        return await item.memo_value(key, None)
    except Exception:  # noqa: BLE001 - an unreadable memo counts as none
        logger.warning("Unreadable memo %s of execution %s", key, item.id, exc_info=True)
        return None


async def _linked_stages(client: Client, parent: str) -> list[str]:
    """The earlier stages of another task that the task flow `parent` links to."""
    try:
        description = await client.get_workflow_handle(parent).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return []
    if description.workflow_type != "TaskWorkflow":
        return []
    linked = await _memo(description, LINKED_STAGES_MEMO)
    return [str(item) for item in linked or [] if isinstance(item, str) and item][:100]


async def _findings_accepted(client: Client, item: WorkflowExecution) -> bool:
    """Whether the open findings of the closed flow's delivery were accepted after it
    closed, which the closed workflow could not record itself."""
    if item.workflow_type == "DeliveryWorkflow":
        session_id = item.id.removeprefix("delivery-")
        run_id = item.run_id
    else:
        session_id = await _memo(item, DELIVERY_SESSION_MEMO)
        if item.workflow_type == "OnboardingWorkflow":
            session_id = item.id
        if not isinstance(session_id, str) or not session_id:
            return False
        try:
            description = await client.get_workflow_handle(delivery_id(session_id)).describe()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            return False
        run_id = description.run_id
    if not run_id:
        return False
    return await acceptances.find(client, session_id, run_id) is not None


async def _execution(item: WorkflowExecution, client: Client | None = None,
                     linked: frozenset[str] = frozenset()) -> dict[str, Any]:
    attributes = item.typed_search_attributes
    repository = await _repository(item)
    branch = repository["branch"] if repository else None
    status = item.status.name.lower() if item.status else "unknown"
    flow_state = attributes.get(FLOW_STATE_ATTRIBUTE)
    if flow_state in ("pending", "skipped"):
        # A stage created before the stages it depends on finished, or one that never ran.
        status = flow_state
    closed = status not in OPEN_STATUSES and status != "pending"
    waiting = attributes.get(WAITING_ATTRIBUTE)
    if waiting is not None and closed:
        waiting = False
    if waiting is None and item.workflow_type in WAITING_TYPES and status in ("pending",
                                                                            "skipped"):
        waiting = False
    unresolved = attributes.get(UNRESOLVED_ATTRIBUTE)
    if unresolved and closed and client is not None and item.workflow_type in FINDINGS_TYPES:
        try:
            if await _findings_accepted(client, item):
                unresolved = False
        except Exception:  # noqa: BLE001 - the marker stays when the record is unreadable
            logger.warning("Acceptance of findings of %s not read", item.id, exc_info=True)
    return {
        "workflow_id": item.id,
        "run_id": item.run_id,
        "workflow_type": item.workflow_type,
        "status": status,
        "task_queue": item.task_queue,
        "parent_workflow_id": item.parent_id,
        "start_time": item.start_time.isoformat(),
        "close_time": item.close_time.isoformat() if item.close_time else None,
        "repository": repository,
        "branch": branch or (attributes.get(BRANCH_ATTRIBUTE) if repository else None),
        "onboarding_id": attributes.get(ONBOARDING_ATTRIBUTE),
        "name": (attributes.get(TASK_NAME_ATTRIBUTE)
                 if item.workflow_type == "TaskWorkflow" else None),
        "linked": item.id in linked,
        "waiting_for_user": waiting if item.workflow_type in WAITING_TYPES else None,
        "unresolved_findings": unresolved if item.workflow_type in FINDINGS_TYPES else None,
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
    linked = await _linked_stages(client, parent) if parent is not None else []
    query = _query(workflow_type, repository, branch, onboarding_id, parent, top_level,
                   linked)
    executions = client.list_workflows(query, page_size=PAGE_SIZE, next_page_token=token)
    await executions.fetch_next_page()
    next_token = executions.next_page_token
    linked_ids = frozenset(linked)
    return {
        "workflows": [await _execution(item, client, linked_ids)
                      for item in executions.current_page or []],
        "next_page_token": base64.urlsafe_b64encode(next_token).decode() if next_token else None,
    }


async def ensure_search_attributes(client: Client) -> None:
    """Register the custom search attributes the executions are started with."""
    listed = await client.operator_service.list_search_attributes(
        ListSearchAttributesRequest(namespace=client.namespace))
    # Each with its own type: keywords, the keyword list of linked task flows and the
    # booleans of the waiting highlight and the findings marker.
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
