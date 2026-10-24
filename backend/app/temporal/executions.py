"""Every workflow execution in the namespace, newest first, from Temporal visibility."""

import base64
import binascii
from typing import Any

from temporalio.client import WorkflowExecution

from app.temporal.client import connect_temporal
from app.temporal.labels import (
    BRANCH_KEY, ONBOARDING_KEY, REPOSITORY_MEMO, filter_query,
)

WORKFLOW_TYPES = (
    "LLMRequestWorkflow",
    "RequirementsWorkflow",
    "ApiDesignWorkflow",
    "OnboardingWorkflow",
    "DeliveryWorkflow",
    "CodegenWorkflow",
    "SayHelloWorkflow",
)
PAGE_SIZE = 50


class InvalidPageToken(ValueError):
    pass


# The types whose executions carry the repository and branch they work in.
LABELLED_TYPES = frozenset({
    "OnboardingWorkflow", "RequirementsWorkflow", "ApiDesignWorkflow", "DeliveryWorkflow",
    "CodegenWorkflow",
})


async def _repository(item: WorkflowExecution) -> dict[str, Any] | None:
    if item.workflow_type not in LABELLED_TYPES:
        return None
    try:
        repository = await item.memo_value(REPOSITORY_MEMO, None)
    except Exception:  # noqa: BLE001 - an unreadable memo only hides the repository
        return None
    return repository if isinstance(repository, dict) else None


async def _execution(item: WorkflowExecution) -> dict[str, Any]:
    attributes = item.typed_search_attributes
    repository = await _repository(item)
    branch = attributes.get(BRANCH_KEY) if item.workflow_type in LABELLED_TYPES else None
    if repository and not branch:
        branch = repository.get("branch")
    return {
        "workflow_id": item.id,
        "run_id": item.run_id,
        "workflow_type": item.workflow_type,
        "status": item.status.name.lower() if item.status else "unknown",
        "task_queue": item.task_queue,
        "parent_workflow_id": item.parent_id,
        "start_time": item.start_time.isoformat(),
        "close_time": item.close_time.isoformat() if item.close_time else None,
        "repository": repository,
        "branch": branch,
        "onboarding_id": attributes.get(ONBOARDING_KEY),
    }


async def list_executions(
    workflow_type: str | None = None,
    page_token: str | None = None,
    repository: str | None = None,
    branch: str | None = None,
    onboarding_id: str | None = None,
) -> dict[str, Any]:
    """A page of executions matching every given filter; the caller validates the filters
    and the type."""
    try:
        token = base64.b64decode(page_token, altchars=b"-_", validate=True) if page_token else None
    except (binascii.Error, ValueError) as exc:
        raise InvalidPageToken("Invalid page token.") from exc
    query = filter_query(workflow_type, repository, branch, onboarding_id)
    client = await connect_temporal()
    executions = client.list_workflows(query, page_size=PAGE_SIZE, next_page_token=token)
    await executions.fetch_next_page()
    next_token = executions.next_page_token
    return {
        "workflows": [await _execution(item) for item in executions.current_page or []],
        "next_page_token": base64.urlsafe_b64encode(next_token).decode() if next_token else None,
    }
