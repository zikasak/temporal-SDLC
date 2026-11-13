"""The LLM names a task flow: from what the intake said needs to be done when the flow
starts, and again from the finalized requirements. A failure never fails the flow, which
keeps the name it has."""

from typing import Any

from langchain_core.messages import HumanMessage
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.llm.graph import final_reply, get_message_graph, native_session
from app.llm.model import get_model
from app.tasks.models import NAME_CHARS
from app.tracing import langchain_config

TASK_CHARS = 4000
REQUIREMENTS_CHARS = 8000

PROMPT = f"""You name a software task in a list of tasks, so that its user recognises it
at a glance. Answer with the name only: one line of at most {NAME_CHARS // 2} characters,
plain text in the language of the task, naming the specific change (for example "Rate
limiting for sign-in" rather than "Service change"); no quotes, no Markdown, no final
full stop, no "Name:" label. Never reveal credentials or secrets."""


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…"


def clean_name(reply: str) -> str:
    """The name in the model's reply; ValueError when it holds none."""
    line = next((item for item in reply.splitlines() if item.strip()), "")
    name = " ".join(line.split()).strip("\"'`*#").strip()
    if name.lower().startswith("name:"):
        name = name[5:].strip()
    name = name.rstrip(".").strip()
    if not name:
        raise ValueError("The LLM wrote no name.")
    if len(name) > NAME_CHARS:
        name = name[:NAME_CHARS - 1].rstrip() + "…"
    return name


@activity.defn
async def name_task_flow(task: str, requirements: list[dict[str, Any]]) -> str:
    """The short name of a task: from `requirements` once they are finalized, otherwise
    from `task`, what the intake said needs to be done."""
    if requirements:
        lines = [f'- {item.get("id", "")}: {item.get("title", "")} - '
                 f'{" ".join(str(item.get("description", "")).split())}'
                 for item in requirements]
        source = ("The task's finalized requirements:\n"
                  + _bounded("\n".join(lines), REQUIREMENTS_CHARS))
        if task:
            source = ("What the user asked for:\n" + _bounded(task, TASK_CHARS)
                      + "\n\n" + source)
    else:
        source = "What needs to be done:\n" + _bounded(task or "(not stated)", TASK_CHARS)
    try:
        model = await get_model("requirements")
        graph = get_message_graph([], model=model)
        async with native_session(model):
            result = await graph.ainvoke(
                {"messages": [HumanMessage(content=f"{PROMPT}\n\n{source}")]},
                config=langchain_config(recursion_limit=4, run_name="name-task-flow"),
            )
        return clean_name(final_reply(result["messages"]))
    except ValueError as exc:
        # The provider's own message may carry credentials; it is only logged by type.
        activity.logger.warning("The LLM could not name the task: %s", type(exc).__name__)
        raise ApplicationError("The LLM could not name the task.", non_retryable=True) from None
    except Exception as exc:
        activity.logger.warning("The LLM could not name the task: %s", type(exc).__name__)
        raise ApplicationError("The LLM could not be reached to name the task.") from None
