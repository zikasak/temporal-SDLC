"""The LLM names a task flow: from the intake's task when the flow starts, and again from
the finalized requirements. A failure never fails the flow, which keeps its current name."""

from typing import Any

from langchain_core.messages import HumanMessage
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.llm.graph import final_reply, get_message_graph, native_session
from app.llm.model import get_model
from app.temporal.execution_fields import TASK_NAME_CHARS, task_name
from app.temporal.tracing import trace_activity
from app.tracing import langchain_config

TASK_CHARS = 6000
REQUIREMENTS_CHARS = 8000
NAME_WORDS = 10
KINDS = {'new_service': 'a new service', 'api_change': 'a change of an API',
         'bugfix': 'a bug fix', 'docs': 'a documentation change'}

PROMPT = f"""You name a task of a software delivery pipeline for a list of tasks. Answer
with the name only: one line of at most {NAME_WORDS} words and {TASK_NAME_CHARS // 2}
characters, in the language the task is written in, naming the specific change (for example
"Rate limiting for sign-in" rather than "Fix the service"). No quotes, no Markdown, no
"Name:" label, no full stop. Never include credentials or secrets."""


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + '\n…'


def _requirements(items: list[dict[str, Any]]) -> str:
    lines = [f'- {item.get("id", "")}: {item.get("title", "")} - '
             f'{" ".join(str(item.get("description", "")).split())}' for item in items]
    return _bounded('\n'.join(lines), REQUIREMENTS_CHARS)


def clean_name(reply: str) -> str:
    """The name in the model's reply: its first non-empty line, without quotes or label."""
    line = next((item.strip() for item in reply.splitlines() if item.strip()), '')
    if line.lower().startswith('name:'):
        line = line[5:].strip()
    line = line.strip('"\'`*# ').rstrip('.')
    return task_name(line)


@activity.defn
@trace_activity('name-task-flow', feature='task-flow')
async def name_task_flow(kind: str | None, task: str,
                         requirements: list[dict[str, Any]]) -> str:
    """A short name for the task of `kind`: from the finalized `requirements` when there
    are any, from the intake's `task` otherwise."""
    parts = [PROMPT, f'The task is {KINDS.get(kind or "", "a software task")}.']
    if task.strip():
        parts.append('What needs to be done, as the user said:\n'
                     + _bounded(task.strip(), TASK_CHARS))
    if requirements:
        parts.append('Its finalized requirements, which the name has to sum up:\n'
                     + _requirements(requirements))
    try:
        model = await get_model('requirements')
        graph = get_message_graph([], model=model)
        async with native_session(model):
            result = await graph.ainvoke(
                {'messages': [HumanMessage(content='\n\n'.join(parts))]},
                config=langchain_config(recursion_limit=4, run_name='name-task-flow'),
            )
    except Exception as exc:
        # The provider's own message may carry credentials; it is only logged by type.
        activity.logger.warning('The LLM could not name the task: %s', type(exc).__name__)
        raise ApplicationError('The LLM could not name the task.', non_retryable=True) from None
    name = clean_name(final_reply(result['messages']))
    if not name:
        raise ApplicationError('The LLM answered without a name.', non_retryable=True)
    return name
