"""Activities of an onboarding: what the branch already has, and the LLM's turns."""

import json
import time
from pathlib import Path
from typing import Any, cast

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.errors import GraphRecursionError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.architecture.models import (
    ArchitectureStructure, architecture_problems, normalize_structure,
    render_architecture_markdown,
)
from app.asyncio_utils import run_sync
from app.config import GENERATED_CODE_DIR
from app.delivery.contract import ContractError, read_architecture, read_service_contract
from app.github import checkout as repository_checkout
from app.llm.graph import final_reply, get_message_graph, interview, native_session
from app.llm.model import get_model
from app.onboarding.models import Artifact, OnboardingState
from app.onboarding.prompt import (
    ARCHITECTURE_FIX_GUIDANCE, architecture_fallback, draft_fallback, turn_messages,
)
from app.temporal.activities import _conversation
from app.temporal.tracing import trace_activity
from app.tools import api_design as api_design_tools
from app.tools import architecture as architecture_tools
from app.tools import questions as question_tools
from app.tools import repository as repository_tools
from app.tools import requirements as requirements_tools
from app.tracing import langchain_config

# The requirements tools of a task's interview that an onboarding has no use for: it has
# no task kind and ends no requirement of an earlier contract.
UNUSED_TOOLS = ("set_task_kind", "retire_requirement", "keep_requirement")
# How much of the model's summary of an architecture fix goes into the reply.
SUMMARY_CHARS = 2000


def _has_files(directory: Path) -> bool:
    return directory.is_dir() and any(item.is_file() for item in directory.rglob("*"))


def find_artifacts(root: Path) -> list[Artifact]:
    """What of the onboarding's results the checkout at `root` already holds."""
    found: list[Artifact] = []
    if _has_files(root / repository_checkout.CONTRACTS_DIR):
        found.append("contract")
    if _has_files(root / repository_checkout.TESTS_DIR):
        found.append("contract_tests")
    if (root / "Dockerfile").is_file():
        found.append("dockerfile")
    return found


@activity.defn
@trace_activity('find-onboarding-artifacts', feature='onboarding', input_parameter='session_id')
async def find_onboarding_artifacts(session_id: str) -> list[str]:
    root = repository_checkout.checkout_path(GENERATED_CODE_DIR, session_id)
    if not await run_sync(root.is_dir):
        raise ApplicationError("The repository checkout is unavailable.")
    return list(await run_sync(find_artifacts, root))


def inspect_contract(root: Path) -> dict[str, Any]:
    """Whether the contract the checkout at `root` holds lacks its architecture, and why the
    rest of it could not be kept as it is, when it cannot."""
    try:
        read_architecture(root)
        missing = False
    except ContractError:
        missing = True
    try:
        read_service_contract(root)
        problem = None
    except ContractError as exc:
        problem = str(exc)
    return {"architecture_missing": missing, "contract_problem": problem}


@activity.defn
@trace_activity('inspect-onboarding-contract', feature='onboarding',
                input_parameter='session_id')
async def inspect_onboarding_contract(session_id: str) -> dict[str, Any]:
    root = repository_checkout.checkout_path(GENERATED_CODE_DIR, session_id)
    if not await run_sync(root.is_dir):
        raise ApplicationError("The repository checkout is unavailable.")
    return await run_sync(inspect_contract, root)


@activity.defn
@trace_activity('onboarding-turn', feature='onboarding', as_type='agent')
async def onboarding_turn(state: OnboardingState) -> dict[str, Any]:
    """Draft the contract from the code (a state without messages), or answer the user's
    last message about the drafts. Returns the reply and the updated drafts."""
    workflow_id = activity.info().workflow_id
    assert workflow_id is not None  # run by the onboarding's workflow
    root = repository_checkout.checkout_path(GENERATED_CODE_DIR, workflow_id)
    repository = (await run_sync(repository_tools.build, root)
                  if await run_sync(root.is_dir) else [])
    if not repository:
        raise ApplicationError("The repository checkout is unavailable.")
    if state.architecture_only:
        return await _architecture_turn(state, root, repository)
    requirements = requirements_tools.RequirementsContext.from_list(state.requirements)
    api = api_design_tools.ApiDesignContext.from_model(state.api, None, state.language)
    # Endpoints link to the requirements as recorded so far in this same turn.
    api.requirement_ids = requirements.requirements.keys()  # type: ignore[assignment]
    architecture = architecture_tools.ArchitectureContext(
        cast(ArchitectureStructure | None, state.architecture_structure))
    asked = question_tools.QuestionsContext()
    tools = [*(item for item in requirements_tools.build(requirements)
               if item.name not in UNUSED_TOOLS),
             *api_design_tools.build(api), *architecture_tools.build(architecture),
             *question_tools.build(asked), *repository]
    started = time.perf_counter()
    try:
        model = await get_model('api_design')
        graph = get_message_graph(tools, until=lambda: bool(asked.questions), model=model)
        async with native_session(model, _conversation("onboarding")):
            reply = await interview(
                graph, turn_messages(state, repository_tools.GUIDANCE),
                langchain_config(recursion_limit=200 if not state.drafted else 120,
                                 run_name='onboarding-with-tools'),
                lambda: bool(asked.questions))
    except GraphRecursionError:
        # Drafting takes many tool calls; keep what was recorded.
        activity.logger.warning("The onboarding turn hit the recursion limit")
        reply = ""
    except ValueError as exc:
        activity.logger.warning("Onboarding turn failed: %s", exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    updated_requirements = requirements.as_list()
    updated_api = api.as_model()
    activity.logger.info("Onboarding turn took %.1fs: %d requirements, %d endpoints",
                         time.perf_counter() - started, len(updated_requirements),
                         len(updated_api["endpoints"]))
    updated_architecture = architecture.result()
    unchanged = (updated_requirements == state.requirements and updated_api == state.api
                 and api.language == state.language
                 and updated_architecture == state.architecture_structure)
    if not reply and not asked.questions:
        if unchanged:
            raise ApplicationError("The model returned no text", non_retryable=True)
        reply = draft_fallback(len(updated_requirements), len(updated_api["endpoints"]),
                               len(updated_api["schemas"]))["content"]
    return {"reply": reply, "questions": asked.questions, "requirements": updated_requirements,
            "api": updated_api, "language": api.language,
            "architecture_structure": updated_architecture, "conflicts": list(state.conflicts)}


@activity.defn
@trace_activity('revise-onboarding-architecture', feature='onboarding', as_type='agent')
async def revise_onboarding_architecture(
    session_id: str, structure: dict[str, Any] | None, instructions: str,
) -> dict[str, Any]:
    """A fix round of an architecture-only onboarding's pull request: change the drafted
    architecture as the reviewer's comment `instructions` asks, checked against the code
    of the onboarding's checkout. Returns the changed structure, its Markdown and the
    model's summary for the reply; fails when the architecture did not change or is
    incomplete afterwards, so that nothing is pushed."""
    root = repository_checkout.checkout_path(GENERATED_CODE_DIR, session_id)
    repository = (await run_sync(repository_tools.build, root)
                  if await run_sync(root.is_dir) else [])
    if not repository:
        raise ApplicationError("The repository checkout is unavailable.")
    try:
        contract = await run_sync(read_service_contract, root)
    except ContractError:
        contract = {}
    current = normalize_structure(structure) if structure else None
    architecture = architecture_tools.ArchitectureContext(current)
    tools = [*architecture_tools.build(architecture), *repository]
    guidance = (
        f"{ARCHITECTURE_FIX_GUIDANCE}"
        f"\n\nExisting requirements: "
        f"{json.dumps(contract.get('requirements', []), ensure_ascii=False)}"
        f"\nExisting API: {json.dumps(contract.get('api', {}), ensure_ascii=False)}"
        f"\nCurrent architecture: {json.dumps(current, ensure_ascii=False)}")
    started = time.perf_counter()
    try:
        model = await get_model('api_design')
        graph = get_message_graph(tools, model=model)
        async with native_session(model):
            result = await graph.ainvoke(
                {"messages": [SystemMessage(content=guidance),
                              HumanMessage(content=f"The comment, in full:\n{instructions}")]},
                config=langchain_config(recursion_limit=150,
                                        run_name='revise-onboarding-architecture'))
        summary = final_reply(result["messages"])
    except GraphRecursionError:
        activity.logger.warning("The architecture fix hit the recursion limit")
        summary = ""
    except ValueError as exc:
        activity.logger.warning("Architecture fix failed: %s", exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    updated = architecture.result()
    activity.logger.info("Architecture fix of %s took %.1fs", session_id,
                         time.perf_counter() - started)
    if updated is None or normalize_structure(updated) == current:
        raise ApplicationError(
            "The comment led to no change of the architecture."
            + (f" {summary[:SUMMARY_CHARS]}" if summary else ""), non_retryable=True)
    updated = normalize_structure(updated)
    problems = architecture_problems(updated)
    if problems:
        raise ApplicationError(
            "The changed architecture is incomplete: " + " ".join(problems[:5]),
            non_retryable=True)
    return {"architecture_structure": dict(updated),
            "architecture": render_architecture_markdown(updated),
            "summary": summary[:SUMMARY_CHARS]}


async def _architecture_turn(state: OnboardingState, root: Path,
                             repository: list) -> dict[str, Any]:
    """Draft only the architecture from the code, checked against the existing contract,
    or answer the user's last message about it."""
    try:
        contract = await run_sync(read_service_contract, root)
    except ContractError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    conflicts = list(state.conflicts)
    architecture = architecture_tools.ArchitectureContext(
        cast(ArchitectureStructure | None, state.architecture_structure), conflicts=conflicts)
    asked = question_tools.QuestionsContext()
    tools = [*architecture_tools.build(architecture), *question_tools.build(asked),
             *repository]
    started = time.perf_counter()
    try:
        model = await get_model('api_design')
        graph = get_message_graph(tools, until=lambda: bool(asked.questions), model=model)
        async with native_session(model, _conversation("onboarding-architecture")):
            reply = await interview(
                graph, turn_messages(state, repository_tools.GUIDANCE, contract=contract),
                langchain_config(recursion_limit=150 if not state.drafted else 120,
                                 run_name='onboarding-architecture-with-tools'),
                lambda: bool(asked.questions))
    except GraphRecursionError:
        activity.logger.warning("The onboarding architecture turn hit the recursion limit")
        reply = ""
    except ValueError as exc:
        activity.logger.warning("Onboarding architecture turn failed: %s", exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    updated = architecture.result()
    activity.logger.info("Onboarding architecture turn took %.1fs: %d components, "
                         "%d conflicts", time.perf_counter() - started,
                         len(updated["components"]) if updated else 0, len(conflicts))
    if not reply and not asked.questions:
        if updated == state.architecture_structure and conflicts == state.conflicts:
            raise ApplicationError("The model returned no text", non_retryable=True)
        reply = architecture_fallback(updated)["content"]
    return {"reply": reply, "questions": asked.questions, "requirements": state.requirements,
            "api": state.api, "language": state.language,
            "architecture_structure": updated, "conflicts": conflicts}
