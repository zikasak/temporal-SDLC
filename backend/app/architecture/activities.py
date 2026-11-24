"""Activities of the architecture session: the LLM's turns and the finalized document."""

import time
from typing import Any

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
from app.temporal.tracing import trace_activity
from app.tools import architecture as architecture_tools
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
    ctx = architecture_tools.ArchitectureContext(
        state.structure, base=state.existing_structure, may_skip=may_skip)
    asked = question_tools.QuestionsContext()
    repository = await run_sync(_repository_tools)
    started = time.perf_counter()
    try:
        # The architecture is drafted with the model of the API design it leads to.
        model = await get_model('api_design')
        graph = get_message_graph(
            [*architecture_tools.build(ctx), *question_tools.build(asked), *repository],
            until=lambda: bool(asked.questions) or bool(ctx.skip_reason), model=model)
        async with native_session(model, _conversation("architecture")):
            reply = await interview(
                graph, turn_messages(state, may_skip,
                                     repository_tools.GUIDANCE if repository else ""),
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
        if structure == state.structure:
            raise ApplicationError("The model returned no text", non_retryable=True)
        reply = draft_fallback(structure) if structure else ""
    if not reply and not asked.questions:
        raise ApplicationError("The model returned no text", non_retryable=True)
    return {"reply": reply, "questions": asked.questions, "structure": structure,
            "skip_reason": None}


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
