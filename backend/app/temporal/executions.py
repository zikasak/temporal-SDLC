"""Every workflow execution in the namespace, newest first, from Temporal visibility."""

import base64
import binascii
from typing import Any

from temporalio.client import WorkflowExecution

from app.temporal.client import connect_temporal

WORKFLOW_TYPES = (
    "LLMRequestWorkflow",
    "RequirementsWorkflow",
    "ApiDesignWorkflow",
    "CodegenWorkflow",
    "SayHelloWorkflow",
)
PAGE_SIZE = 50


class InvalidPageToken(ValueError):
    pass


def _execution(item: WorkflowExecution) -> dict[str, Any]:
    return {
        "workflow_id": item.id,
        "run_id": item.run_id,
        "workflow_type": item.workflow_type,
        "status": item.status.name.lower() if item.status else "unknown",
        "task_queue": item.task_queue,
        "parent_workflow_id": item.parent_id,
        "start_time": item.start_time.isoformat(),
        "close_time": item.close_time.isoformat() if item.close_time else None,
    }


async def list_executions(
    workflow_type: str | None = None, page_token: str | None = None
) -> dict[str, Any]:
    try:
        token = base64.b64decode(page_token, altchars=b"-_", validate=True) if page_token else None
    except (binascii.Error, ValueError) as exc:
        raise InvalidPageToken("Invalid page token.") from exc
    # workflow_type is checked against WORKFLOW_TYPES, so it is safe to inline.
    query = f"WorkflowType = '{workflow_type}'" if workflow_type else None
    client = await connect_temporal()
    executions = client.list_workflows(query, page_size=PAGE_SIZE, next_page_token=token)
    await executions.fetch_next_page()
    next_token = executions.next_page_token
    return {
        "workflows": [_execution(item) for item in executions.current_page or []],
        "next_page_token": base64.urlsafe_b64encode(next_token).decode() if next_token else None,
    }
