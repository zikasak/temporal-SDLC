"""Activities of an onboarding: what the branch already has, and the LLM's turns."""

import time
from pathlib import Path
from typing import Any

from langgraph.errors import GraphRecursionError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.asyncio_utils import run_sync
from app.config import GENERATED_CODE_DIR
from app.github import checkout as repository_checkout
from app.llm.graph import get_message_graph, interview, native_session
from app.llm.model import get_model
from app.onboarding.models import Artifact, OnboardingState
from app.onboarding.prompt import draft_fallback, turn_messages
from app.temporal.tracing import trace_activity
from app.tools import api_design as api_design_tools
from app.tools import questions as question_tools
from app.tools import repository as repository_tools
from app.tools import requirements as requirements_tools
from app.tracing import langchain_config

# The requirements tools of a task's interview that an onboarding has no use for: it has
# no task kind and ends no requirement of an earlier contract.
UNUSED_TOOLS = ("set_task_kind", "retire_requirement", "keep_requirement")


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


@activity.defn
@trace_activity('onboarding-turn', feature='onboarding', as_type='agent')
async def onboarding_turn(state: OnboardingState) -> dict[str, Any]:
    """Draft the contract from the code (a state without messages), or answer the user's
    last message about the drafts. Returns the reply and the updated drafts."""
    root = repository_checkout.checkout_path(GENERATED_CODE_DIR, activity.info().workflow_id)
    repository = (await run_sync(repository_tools.build, root)
                  if await run_sync(root.is_dir) else [])
    if not repository:
        raise ApplicationError("The repository checkout is unavailable.")
    requirements = requirements_tools.RequirementsContext.from_list(state.requirements)
    api = api_design_tools.ApiDesignContext.from_model(state.api, None, state.language)
    # Endpoints link to the requirements as recorded so far in this same turn.
    api.requirement_ids = requirements.requirements.keys()  # type: ignore[assignment]
    asked = question_tools.QuestionsContext()
    tools = [*(item for item in requirements_tools.build(requirements)
               if item.name not in UNUSED_TOOLS),
             *api_design_tools.build(api), *question_tools.build(asked), *repository]
    started = time.perf_counter()
    try:
        model = await get_model('api_design')
        graph = get_message_graph(tools, until=lambda: bool(asked.questions), model=model)
        async with native_session(model):
            reply = await interview(
                graph, turn_messages(state, repository_tools.GUIDANCE),
                langchain_config(recursion_limit=80 if not state.drafted else 40,
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
    unchanged = (updated_requirements == state.requirements and updated_api == state.api
                 and api.language == state.language)
    if not reply and not asked.questions:
        if unchanged:
            raise ApplicationError("The model returned no text", non_retryable=True)
        reply = draft_fallback(len(updated_requirements), len(updated_api["endpoints"]),
                               len(updated_api["schemas"]))["content"]
    return {"reply": reply, "questions": asked.questions, "requirements": updated_requirements,
            "api": updated_api, "language": api.language}
