"""The read-requirements tool every agent running in the workers is offered: it reads the
requirements store for the agent's own task flow, never another one's. Agent containers,
without database access, get the same tool over a snapshot of those requirements
(app.agent_workflows.runtime.requirements_tools)."""

import json
import logging

import psycopg
from langchain_core.tools import BaseTool, tool

from app.requirements import store

logger = logging.getLogger(__name__)

TOOL_NAME = "read_requirements"
# The most requirements one call returns, so that a large contract fits the context.
MAX_RESULTS = 100
DESCRIPTION = """Read the project's requirements: those the branch's contract already had
and those created or changed during this task, each with its ID, category, title,
description, priority, component (the architecture component it belongs to, or null),
origin (baseline, created, changed, assigned or retired) and whether this task retires it.
Filter by `component` (a component name, or "none" for requirements without one), by
requirement `id`, or by free-text `search` over ID, title and description; with no filter
every requirement is returned (at most 100; narrow the filter for more)."""


def result_text(items: list[store.StoredRequirement] | None) -> str:
    if items is None:
        return "No requirements are stored for this task."
    shown = items[:MAX_RESULTS]
    payload: dict[str, object] = {"requirements": shown, "count": len(items)}
    if len(items) > MAX_RESULTS:
        payload["note"] = (f"Only the first {MAX_RESULTS} of {len(items)} are shown; "
                           "filter by component, id or search to see the rest.")
    return json.dumps(payload, ensure_ascii=False)


def build(task_flow_id: str | None) -> list[BaseTool]:
    """The tool bound to `task_flow_id`; none for an agent outside a task flow."""
    if not task_flow_id:
        return []

    @tool(TOOL_NAME, description=DESCRIPTION)
    async def read_requirements(component: str | None = None, id: str | None = None,
                                search: str | None = None) -> str:
        try:
            items = await store.read(task_flow_id, component=component,
                                     requirement_id=id, search=search)
        except (OSError, psycopg.Error) as exc:
            logger.warning("Could not read the requirements of %s: %s", task_flow_id, exc)
            return "The requirements store is unavailable right now; try again later."
        return result_text(items)

    return [read_requirements]
