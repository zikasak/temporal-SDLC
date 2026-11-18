"""Activities of task flows: the LLM names the flow, and the earlier stages a fork or fix
round links to are checked for having closed."""

from langchain_core.messages import HumanMessage
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from app.llm.graph import final_reply, get_message_graph, native_session
from app.llm.model import get_model
from app.tasks.models import NAME_CHARS
from app.temporal.client import connect_temporal
from app.temporal.tracing import trace_activity
from app.tracing import langchain_config

# Bounds of what the LLM receives.
TEXT_CHARS = 8000

PROMPT = f"""Write a short name for a software task, from what the user said needs to be
done or from its requirements below. The name is shown in a list of the user's tasks, so it
names the specific change, such as "Rate-limit sign-in attempts" or "Room booking
service", not "New task".

Answer with the name alone: one line of at most {NAME_CHARS} characters, plain text, no
quotes, no Markdown and no trailing full stop. Never reveal credentials or secrets.

The task:
"""


def clean_name(text: str) -> str:
    """The first line of the LLM's answer as a name: without quotes, labels or Markdown,
    and at most NAME_CHARS long; empty when there is none."""
    line = next((item for item in str(text or "").splitlines() if item.strip()), "")
    line = " ".join(line.split()).strip("*_`#> ")
    for label in ("Name:", "Title:", "name:", "title:"):
        if line.startswith(label):
            line = line[len(label):].strip()
    line = line.strip("\"'“”‘’ ").rstrip(".")
    if len(line) > NAME_CHARS:
        line = line[: NAME_CHARS - 1].rstrip() + "…"
    return line


@activity.defn
@trace_activity('name-task-flow', feature='tasks', input_parameter='text')
async def name_task_flow(text: str) -> str:
    """The task flow's short name, written by the LLM from `text`."""
    text = str(text or "")
    if len(text) > TEXT_CHARS:
        text = text[:TEXT_CHARS] + "\n…"
    try:
        model = await get_model('requirements')
        graph = get_message_graph([], model=model)
        async with native_session(model):
            result = await graph.ainvoke(
                {'messages': [HumanMessage(content=PROMPT + text)]},
                config=langchain_config(recursion_limit=4, run_name='name-task-flow'),
            )
    except ValueError as exc:
        # The provider's own message may carry credentials; it is only named.
        activity.logger.warning('The LLM could not name the task flow: %s', type(exc).__name__)
        raise ApplicationError('The LLM is not configured or rejected the request.',
                               non_retryable=True) from None
    except Exception as exc:  # noqa: BLE001 - retried, then the flow keeps its name
        activity.logger.warning('The LLM could not name the task flow: %s', type(exc).__name__)
        raise ApplicationError('The LLM could not be reached or failed to answer.') from None
    name = clean_name(final_reply(result['messages']))
    if not name:
        raise ApplicationError('The LLM wrote no name.', non_retryable=True)
    return name


@activity.defn
async def open_workflows(workflow_ids: list[str]) -> list[str]:
    """Those of the workflows whose latest run is still open; a missing one is closed."""
    client = await connect_temporal()
    found = []
    for workflow_id in workflow_ids:
        try:
            description = await client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                continue
            raise
        if description.status == WorkflowExecutionStatus.RUNNING:
            found.append(workflow_id)
    return found
