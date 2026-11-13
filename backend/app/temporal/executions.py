"""Every workflow execution in the namespace, newest first, from Temporal visibility."""

import base64
import binascii
import logging
import re
from collections.abc import Mapping
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
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import (
    BRANCH_ATTRIBUTE,
    DELIVERY_MEMO,
    FINDINGS_MEMO,
    FLOW_STATUS_MEMO,
    LINKED_MEMO,
    NAME_MEMO,
    ONBOARDING_ATTRIBUTE,
    PENDING,
    REPOSITORY_ATTRIBUTE,
    REPOSITORY_MEMO,
    SEARCH_ATTRIBUTES,
    SKIPPED,
    WAITING_MEMO,
)

logger = logging.getLogger(__name__)

TASK_FLOW_TYPE = "TaskWorkflow"
# How the workflow IDs of task flows begin (app.tasks.models.ID_PREFIX).
TASK_FLOW_PREFIX = "task-flow-"
WORKFLOW_TYPES = (
    TASK_FLOW_TYPE,
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


def _parent_clause(parent: str, description: Any | None, children: list[str]) -> str:
    """The executions listed under `parent`: its direct children and the executions named
    in `children`, such as a task flow's delivery started again on its own and the earlier
    stages of another task that a forked or fix-round task flow links to. A session from
    before task flows existed also lists its delivery started again on its own, which a
    stage of a task flow never does: that delivery is a stage of the task itself."""
    clause = f"ParentWorkflowId = {_quote(parent)}"
    named = list(children)
    stage_of_task = bool(description is not None and description.parent_id
                         and description.parent_id.startswith(TASK_FLOW_PREFIX))
    if not stage_of_task and (description is None
                              or description.workflow_type != TASK_FLOW_TYPE):
        named.append(delivery_id(parent))
    if not named:
        return clause
    listed = ", ".join(_quote(item) for item in dict.fromkeys(named))
    return f"({clause} OR WorkflowId IN ({listed}))"


def _query(workflow_type: str | None, repository: str | None, branch: str | None,
           onboarding_id: str | None, parent: str | None = None,
           top_level: bool = False, parent_clause: str | None = None) -> str | None:
    # Records of acceptances of closed deliveries' findings are not flows.
    clauses = [f"WorkflowType != '{ACCEPTANCE_RECORD_TYPE}'"]
    # A session's delivery is its child, except for a run started again on its own after
    # the session closed: it is listed under the session by its ID, never at the top. A
    # flow that carries on a closed onboarding's delivery, such as a fix a pull request
    # comment asked for after the pull request was merged, is a top-level flow of its own;
    # a task's is the child of a task flow of its own. No stage of a task flow is ever
    # top-level.
    if top_level:
        clauses.append("ParentWorkflowId IS NULL AND (WorkflowType != 'DeliveryWorkflow' "
                       f"OR WorkflowId STARTS_WITH '{delivery_id(FOLLOW_UP_PREFIX)}')")
    if parent is not None:
        clauses.append(parent_clause or _parent_clause(parent, None, []))
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


async def _memo(item: Any) -> dict[str, Any]:
    try:
        memo = await item.memo()
    except Exception:  # noqa: BLE001 - an unreadable memo lists without its fields
        logger.warning("Unreadable memo of execution %s", item.id, exc_info=True)
        return {}
    return dict(memo) if isinstance(memo, Mapping) else {}


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


async def _accepted_after_close(client: Client, delivery_workflow_id: str,
                                run_id: str | None = None) -> bool:
    """Whether the open findings of a closed delivery were accepted after it closed: its
    workflow could not record that, so the acceptance is kept apart (app.delivery
    .acceptances). The delivery's latest run when `run_id` is None."""
    session_id = delivery_workflow_id.removeprefix("delivery-")
    try:
        if run_id is None:
            description = await client.get_workflow_handle(delivery_workflow_id).describe()
            if description.status == WorkflowExecutionStatus.RUNNING:
                return False
            run_id = description.run_id
        if not run_id:
            return False
        return await acceptances.find(client, session_id, run_id) is not None
    except RPCError as exc:
        logger.warning("Acceptance of the findings of %s not read: %s",
                       delivery_workflow_id, exc)
        return False


async def _unresolved(client: Client, item: WorkflowExecution, memo: dict[str, Any],
                      running: bool) -> bool | None:
    """Whether the flow's delivery has open review findings that were not accepted; None
    for an execution without a delivery or from before it was reported."""
    value = _flag(memo.get(FINDINGS_MEMO))
    if not value or running:
        return value
    # Closed with findings raised: the user may have accepted them since.
    if item.workflow_type == "DeliveryWorkflow":
        accepted = await _accepted_after_close(client, item.id, item.run_id)
    elif item.workflow_type == TASK_FLOW_TYPE and isinstance(memo.get(DELIVERY_MEMO), str):
        accepted = await _accepted_after_close(client, memo[DELIVERY_MEMO])
    elif item.workflow_type == "OnboardingWorkflow":
        accepted = await _accepted_after_close(client, delivery_id(item.id))
    else:
        accepted = False
    return not accepted


async def _execution(item: WorkflowExecution, client: Client | None = None,
                     linked: frozenset[str] = frozenset()) -> dict[str, Any]:
    attributes = item.typed_search_attributes
    repository = await _repository(item)
    branch = repository["branch"] if repository else None
    memo = await _memo(item)
    running = item.status == WorkflowExecutionStatus.RUNNING
    status = item.status.name.lower() if item.status else "unknown"
    flow_status = memo.get(FLOW_STATUS_MEMO)
    # A stage of a task flow that waits for the stages before it, or will never run.
    if flow_status == PENDING and running:
        status = PENDING
    elif flow_status == SKIPPED and item.status == WorkflowExecutionStatus.COMPLETED:
        status = SKIPPED
    waiting = _flag(memo.get(WAITING_MEMO))
    if waiting is not None and not running:
        # A closed flow waits for nothing.
        waiting = False
    name = memo.get(NAME_MEMO) if item.workflow_type == TASK_FLOW_TYPE else None
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
        "name": name if isinstance(name, str) and name else None,
        "linked": item.id in linked,
        "waiting_for_user": waiting,
        "unresolved_findings": (await _unresolved(client, item, memo, running)
                                if client is not None else _flag(memo.get(FINDINGS_MEMO))),
    }


async def _listed_under(client: Client, parent: str) -> tuple[str, frozenset[str]]:
    """The query clause of the executions listed under `parent`, and those of them that are
    earlier stages of another task that the task flow `parent` links to."""
    try:
        description = await client.get_workflow_handle(parent).describe()
    except RPCError as exc:
        if exc.status not in (RPCStatusCode.NOT_FOUND, RPCStatusCode.INVALID_ARGUMENT):
            raise
        return _parent_clause(parent, None, []), frozenset()
    if description.workflow_type != TASK_FLOW_TYPE:
        return _parent_clause(parent, description, []), frozenset()
    try:
        linked = await description.memo_value(LINKED_MEMO, [])
        delivery = await description.memo_value(DELIVERY_MEMO, None)
    except Exception:  # noqa: BLE001 - an unreadable memo lists only the children
        logger.warning("Unreadable memo of task flow %s", parent, exc_info=True)
        linked, delivery = [], None
    linked_ids = [item for item in linked or [] if isinstance(item, str) and item]
    # A task flow's delivery started again on its own is no longer its child, but listed.
    named = [*([delivery] if isinstance(delivery, str) and delivery else []), *linked_ids]
    return _parent_clause(parent, description, named), frozenset(linked_ids)


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
    parent_clause, linked = (await _listed_under(client, parent) if parent is not None
                             else (None, frozenset()))
    query = _query(workflow_type, repository, branch, onboarding_id, parent, top_level,
                   parent_clause)
    executions = client.list_workflows(query, page_size=PAGE_SIZE, next_page_token=token)
    await executions.fetch_next_page()
    next_token = executions.next_page_token
    return {
        "workflows": [await _execution(item, client, linked)
                      for item in executions.current_page or []],
        "next_page_token": base64.urlsafe_b64encode(next_token).decode() if next_token else None,
    }


async def ensure_search_attributes(client: Client) -> None:
    """Register the custom search attributes the executions are started with."""
    listed = await client.operator_service.list_search_attributes(
        ListSearchAttributesRequest(namespace=client.namespace))
    missing = {key.name: IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD
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
