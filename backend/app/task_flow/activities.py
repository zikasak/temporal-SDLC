"""Activities of task flows: the LLM writes a task flow's short name, and the flow finds
out which earlier stages of another task it links to have closed."""

import logging

from langchain_core.messages import HumanMessage
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from app.llm.graph import final_reply, get_message_graph, native_session
from app.llm.model import get_model
from app.task_flow.models import NAME_CHARS, clean_name
from app.temporal.client import connect_temporal
from app.temporal.tracing import trace_activity
from app.tracing import langchain_config

logger = logging.getLogger(__name__)

# How much of the task or its requirements the LLM receives.
SOURCE_CHARS = 8000

PROMPT = f"""You name a software task in a list of tasks. Write one short name, at most
{NAME_CHARS} characters and ideally three to eight words, that says what the change does,
such as "Rate-limit sign-in attempts" or "Export invoices as CSV". Answer with the name
only: plain text on one line, no quotes, no Markdown, no label and no trailing period.
Never include credentials or secrets.

The task:
"""


@activity.defn
@trace_activity('name-task-flow', feature='requirements', input_parameter='text')
async def name_task_flow(text: str) -> str:
    """A short name for the task that `text` describes: what needs to be done, as the
    intake said, or the finalized requirements."""
    source = str(text or "").strip()[:SOURCE_CHARS]
    if not source:
        raise ApplicationError("There is nothing to name the task from.", non_retryable=True)
    try:
        model = await get_model('requirements')
        graph = get_message_graph([], model=model)
        async with native_session(model):
            result = await graph.ainvoke(
                {'messages': [HumanMessage(content=PROMPT + source)]},
                config=langchain_config(recursion_limit=4, run_name='name-task-flow'),
            )
    except ValueError as exc:
        # The provider's own message may carry credentials; it is only logged by type.
        activity.logger.warning('The LLM could not name the task: %s', type(exc).__name__)
        raise ApplicationError('The LLM is not configured or rejected the request.',
                               non_retryable=True) from None
    except Exception as exc:  # noqa: BLE001 - reported without the provider's message
        activity.logger.warning('The LLM could not name the task: %s', type(exc).__name__)
        raise ApplicationError('The LLM could not be reached or failed to answer.') from None
    name = clean_name(final_reply(result['messages']))
    if not name:
        raise ApplicationError('The LLM answered without a name.', non_retryable=True)
    return name


@activity.defn
async def closed_workflows(workflow_ids: list[str]) -> list[str]:
    """Those of `workflow_ids` whose latest run is not running, or that do not exist."""
    client = await connect_temporal()
    closed = []
    for workflow_id in workflow_ids:
        try:
            description = await client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            closed.append(workflow_id)
            continue
        if description.status != WorkflowExecutionStatus.RUNNING:
            closed.append(workflow_id)
    return closed
