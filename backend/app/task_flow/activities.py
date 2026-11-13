"""Activities of task flows: their short name, written by the LLM, and whether the
earlier stages a forked or fix-round task flow links to have closed."""

from typing import Any

from langchain_core.messages import HumanMessage
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from app.llm.graph import final_reply, get_message_graph, native_session
from app.llm.model import get_model
from app.task_flow.models import NAME_CHARS
from app.temporal.client import connect_temporal
from app.temporal.tracing import trace_activity
from app.tracing import langchain_config

TASK_CHARS = 6000
REQUIREMENTS_CHARS = 8000
NAME_ERROR = "TaskNameError"

PROMPT = f"""Name a software task in a few words, as a list of tasks shows it. The name
says what changes, specifically (for example "Rate-limit sign-in attempts" or "Add
project export endpoint"), in at most {NAME_CHARS} characters of plain text: no quotes,
no trailing full stop, no Markdown and no "Name:" label. Answer with the name only.
Never reveal credentials or secrets."""


def _clean(reply: str) -> str:
    """The first line of the reply, without quotes, labels or Markdown."""
    line = next((item.strip() for item in reply.splitlines() if item.strip()), "")
    for prefix in ("name:", "title:"):
        if line.lower().startswith(prefix):
            line = line[len(prefix):].strip()
    line = line.strip("*_`#\"'“”‘’ ").rstrip(".").strip()
    line = " ".join(line.split())
    if len(line) > NAME_CHARS:
        line = line[: NAME_CHARS - 1].rstrip() + "…"
    return line


async def _ask(text: str, run_name: str) -> str:
    try:
        model = await get_model("requirements")
        graph = get_message_graph([], model=model)
        async with native_session(model):
            result = await graph.ainvoke(
                {"messages": [HumanMessage(content=text)]},
                config=langchain_config(recursion_limit=4, run_name=run_name),
            )
    except ValueError:
        # The provider's own message may carry credentials; it is only logged by name.
        raise ApplicationError("The LLM is not configured or rejected the request.",
                               type=NAME_ERROR, non_retryable=True) from None
    except Exception as exc:
        activity.logger.warning("The LLM could not name the task: %s", type(exc).__name__)
        raise ApplicationError("The LLM could not be reached or failed to answer.",
                               type=NAME_ERROR) from None
    name = _clean(final_reply(result["messages"]))
    if not name:
        raise ApplicationError("The LLM gave no name.", type=NAME_ERROR, non_retryable=True)
    return name


@activity.defn
@trace_activity("name-task-flow", feature="task-flow")
async def name_task_flow(task: str, kind: str | None) -> str:
    """The task flow's name from what the intake says needs to be done."""
    text = task if len(task) <= TASK_CHARS else task[:TASK_CHARS] + "\n…"
    return await _ask(f"{PROMPT}\n\nThe kind of task: {kind or 'unknown'}.\n\n"
                      f"What needs to be done:\n{text}", "name-task-flow")


@activity.defn
@trace_activity("rename-task-flow", feature="task-flow")
async def rename_task_flow(current: str, requirements: list[dict[str, Any]],
                           retired: list[dict[str, Any]]) -> str:
    """The task flow's name from its finalized requirements."""
    lines = [f"- {item.get('id', '')}: {item.get('title', '')}" for item in requirements]
    lines += [f"- ends {item.get('id', '')}: {item.get('title', '')}" for item in retired]
    listed = "\n".join(lines)
    if len(listed) > REQUIREMENTS_CHARS:
        listed = listed[:REQUIREMENTS_CHARS] + "\n…"
    return await _ask(f"{PROMPT}\n\nThe task's current name: {current or '(none)'}\n\n"
                      f"Its finalized requirements:\n{listed or '(none)'}", "rename-task-flow")


@activity.defn
async def running_workflows(workflow_ids: list[str]) -> list[str]:
    """Those of the workflows whose latest run is still open."""
    client = await connect_temporal()
    running = []
    for workflow_id in workflow_ids:
        try:
            description = await client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            continue
        if description.status == WorkflowExecutionStatus.RUNNING:
            running.append(workflow_id)
    return running
