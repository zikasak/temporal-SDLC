"""Activities of the architecture session: the LLM's turns and the finalized document."""

import time
from functools import partial
from typing import Any

import psycopg
from langgraph.errors import GraphRecursionError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.architecture.models import (
    JSON_FILENAME,
    MARKDOWN_FILENAME,
    ArchitectureState,
    architecture_problems,
    can_skip,
    normalize_structure,
    render_architecture_json,
    render_architecture_markdown,
)
from app.architecture.prompt import draft_fallback, turn_messages
from app.asyncio_utils import run_sync
from app.llm.graph import get_message_graph, interview, native_session
from app.llm.model import get_model
from app.temporal.activities import _conversation, _repository_tools, _save_session_document
from app.requirements import store as requirements_store
from app.temporal.tracing import trace_activity
from app.tools import architecture as architecture_tools
from app.tools import requirements_read as requirements_read_tools
from app.tools import questions as question_tools
from app.tools import repository as repository_tools
from app.llm.limits import AGENT_RECURSION_LIMIT
from app.tracing import langchain_config


@activity.defn
@trace_activity('design-architecture', feature='architecture', as_type='agent')
async def architecture_turn(state: ArchitectureState) -> dict[str, Any]:
    """Draft the architecture (a session without messages), or answer the user's last
    message about it. Returns the reply, the questions, the draft and, when the model
    proposed skipping the stage instead of drafting, its reason."""
    drafting = state.structure is None
    may_skip = (can_skip(state) and not state.skip_declined and drafting
                and not any(item["role"] == "assistant" for item in state.messages))
    task_flow_id = await requirements_store.activity_task_flow()
    requirements = await _requirements_with_components(task_flow_id, state.requirements)
    ctx = architecture_tools.ArchitectureContext(
        state.structure, base=state.existing_structure, may_skip=may_skip,
        requirements=requirements)
    if task_flow_id:
        ctx.persist = partial(requirements_store.assign_components, task_flow_id)
    asked = question_tools.QuestionsContext()
    repository = await run_sync(_repository_tools)
    started = time.perf_counter()
    try:
        # The architecture is drafted with the model of the API design it leads to.
        model = await get_model('api_design')
        graph = get_message_graph(
            [*architecture_tools.build(ctx), *question_tools.build(asked), *repository,
             *requirements_read_tools.build(task_flow_id)],
            until=lambda: bool(asked.questions) or bool(ctx.skip_reason), model=model)
        async with native_session(model, _conversation("architecture")):
            reply = await interview(
                graph, turn_messages(state, may_skip,
                                     (repository_tools.GUIDANCE if repository else "")
                                     + ASSIGNMENT_GUIDANCE),
                langchain_config(recursion_limit=AGENT_RECURSION_LIMIT, run_name='draft-architecture-with-tools'),
                lambda: bool(asked.questions) or bool(ctx.skip_reason))
    except GraphRecursionError:
        activity.logger.warning("The architecture turn hit the recursion limit")
        reply = ""
    except ValueError as exc:
        activity.logger.warning("Architecture turn failed: %s", exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    if ctx.skip_reason:
        activity.logger.info("The architect proposed skipping the stage")
        return {"reply": reply or f"I propose skipping the architecture stage: "
                                  f"{ctx.skip_reason}",
                "questions": asked.questions, "structure": state.structure,
                "skip_reason": ctx.skip_reason}
    structure = ctx.result()
    if structure is None and drafting and state.existing_structure is not None:
        # A change whose draft needed nothing new keeps the existing architecture.
        structure = state.existing_structure
    activity.logger.info("Architecture turn took %.1fs: %d components",
                         time.perf_counter() - started,
                         len(structure["components"]) if structure else 0)
    if not reply and not asked.questions:
        if structure == state.structure and ctx.requirements == requirements:
            raise ApplicationError("The model returned no text", non_retryable=True)
        reply = draft_fallback(structure) if structure else ""
    if not reply and not asked.questions:
        raise ApplicationError("The model returned no text", non_retryable=True)
    # The task's requirements, each with the component the architect assigned it.
    return {"reply": reply, "questions": asked.questions, "structure": structure,
            "skip_reason": None, "requirements": ctx.requirements}


# What the architect is told about grouping the task's requirements by component.
ASSIGNMENT_GUIDANCE = (
    "\nOnce the architecture has components, assign every requirement of the task that "
    "has no component yet to the component responsible for it with assign_requirements; "
    "the tool results list them under requirements_without_component. "
    "Use list_requirement_components to see the assignments so far."
)


async def _requirements_with_components(task_flow_id: str | None,
                                        requirements: list[Any]) -> list[dict[str, Any]]:
    """The task's requirements, each with its component: its own, else the one the task
    flow's requirements store holds, such as one inferred for the branch's contract."""
    records = [dict(item) for item in requirements]
    if not task_flow_id:
        return records
    try:
        stored = await requirements_store.components_of(task_flow_id) or {}
    except (OSError, psycopg.Error) as exc:
        activity.logger.warning("Components of task flow %s unavailable: %s",
                                task_flow_id, exc)
        return records
    for item in records:
        item["component"] = item.get("component") or stored.get(str(item.get("id")))
    return records


@activity.defn
@trace_activity('write-architecture', feature='architecture', input_parameter='structure')
async def write_architecture_document(session_id: str, structure: dict[str, Any],
                                      ) -> dict[str, Any]:
    """Save architecture.md and architecture.json as the session's documents; the
    Markdown document's record, with the JSON one's under `json`."""
    try:
        normalized = normalize_structure(structure)
    except (TypeError, ValueError, KeyError) as exc:
        raise ApplicationError(f"The architecture is malformed: {exc}",
                               non_retryable=True) from exc
    problems = architecture_problems(normalized)
    if problems:
        raise ApplicationError(" ".join(problems), non_retryable=True)
    markdown = await _save_session_document(session_id, MARKDOWN_FILENAME,
                                            render_architecture_markdown(normalized))
    machine = await _save_session_document(session_id, JSON_FILENAME,
                                           render_architecture_json(normalized))
    return {**markdown, "json": machine}
