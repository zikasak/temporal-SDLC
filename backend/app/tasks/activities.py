"""The LLM names a task flow: from the intake's "what needs to be done" when the task
starts, and again from the finalized requirements."""

from typing import Any

from langchain_core.messages import HumanMessage
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.llm.graph import final_reply, get_message_graph, native_session
from app.llm.model import get_model
from app.tasks.models import NAME_CHARS, fallback_name
from app.temporal.tracing import trace_activity
from app.tracing import langchain_config

# Bounds of what the LLM receives.
TASK_CHARS = 4000
REQUIREMENTS_CHARS = 8000

PROMPT = f"""You name a software task for a list of tasks. Write one short name, at most
{NAME_CHARS} characters and ideally three to eight words, that says what the change is
(for example "Rate limiting for sign-in" or "Room booking service"). Answer with the name
alone: plain text on one line, no quotes, no Markdown, no trailing period, no "Name:"
label. Never reveal credentials or secrets."""


def _requirements_text(requirements: list[dict[str, Any]]) -> str:
    lines = []
    for item in requirements:
        lines.append(f'- {item.get("id", "")}: {item.get("title", "")} - '
                     f'{" ".join(str(item.get("description", "")).split())}')
    text = "\n".join(lines)
    return text if len(text) <= REQUIREMENTS_CHARS else text[:REQUIREMENTS_CHARS] + "\n…"


def clean_name(text: str) -> str:
    """The name in the LLM's answer: its first non-empty line, unquoted and bounded."""
    for line in str(text or "").splitlines():
        line = " ".join(line.split()).strip().strip("\"'`*#").strip()
        if line.lower().startswith("name:"):
            line = line[5:].strip()
        line = line.rstrip(".").strip()
        if line:
            return fallback_name(line)
    raise ValueError("The LLM wrote no name.")


@activity.defn
@trace_activity('name-task-flow', feature='tasks', input_parameter='task')
async def name_task_flow(task: str, requirements: list[dict[str, Any]]) -> str:
    """A short name of the task from what needs to be done (`task`) or, once they are
    finalized, from its `requirements`."""
    if requirements:
        source = "The task's finalized requirements:\n" + _requirements_text(requirements)
    else:
        bounded = task if len(task) <= TASK_CHARS else task[:TASK_CHARS] + "…"
        source = "What needs to be done:\n" + bounded
    try:
        model = await get_model("requirements")
        graph = get_message_graph([], model=model)
        async with native_session(model):
            result = await graph.ainvoke(
                {"messages": [HumanMessage(content=f"{PROMPT}\n\n{source}")]},
                config=langchain_config(recursion_limit=10, run_name="name-task-flow"),
            )
        return clean_name(final_reply(result["messages"]))
    except ValueError as exc:
        activity.logger.warning("The task flow could not be named: %s", type(exc).__name__)
        raise ApplicationError("The LLM could not name the task.", non_retryable=True) from None
    except Exception as exc:
        activity.logger.warning("The task flow could not be named: %s", type(exc).__name__)
        raise ApplicationError("The LLM could not be reached to name the task.") from None
