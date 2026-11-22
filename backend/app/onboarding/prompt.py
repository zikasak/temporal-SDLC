"""Prompts of an onboarding: drafting the contract from the code, then reviewing it."""

import json
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.api_design.prompt import language_guidance
from app.architecture.prompt import TOOLS_GUIDANCE as ARCHITECTURE_TOOLS_GUIDANCE
from app.conversations.models import ConversationMessage
from app.languages import describe_supported
from app.llm.prompt import CURRENT_STATE_GUIDANCE, with_current_state
from app.llm.questions import message_text
from app.onboarding.models import OnboardingState, onboarding_problems
from app.tools.questions import GUIDANCE as QUESTIONS_GUIDANCE

GUIDANCE = (
    "You are a senior engineer onboarding an existing service onto a contract-driven "
    "pipeline. The service's Git repository is checked out for you; read it with "
    "list_files, search_files and read_file. Paths are relative to the repository root. "
    "From the code as it is, you record the service's contract with tools: its "
    "requirements (record_requirement, remove_requirement, list_requirements), its HTTP "
    "API (set_api_info, record_schema, record_endpoint, remove_schema, remove_endpoint, "
    "show_api), the language it is written in (set_language) and its top-level "
    "architecture: its components or modules with their responsibilities, the data flow "
    "between them and the key technology choices. " + ARCHITECTURE_TOOLS_GUIDANCE + "\n"
    "The contract describes what the existing code does, not what it should do: every "
    "requirement and every endpoint, schema, field, status code and error status must be "
    "backed by code you read. Contract tests are written from it and must pass against "
    "the code as it is, so never invent behaviour, and leave out what you could not "
    "confirm in the code. Each requirement is one testable statement. Link each endpoint "
    "to the requirements it serves with requirement_ids. When a tool call fails, fix the "
    "arguments and call it again; only report changes whose tool call succeeded. Do not "
    "paste the contract back to the user; they can see it."
)

DRAFT_MESSAGE = (
    "Draft the contract of the service in this repository from its existing code: read "
    "the code first, record the language with set_language, the requirements, the "
    "whole HTTP API it serves and its architecture, then briefly describe the draft, say "
    "what you were unsure about and ask your first questions with ask_user."
)

REVIEW_GUIDANCE = (
    "\nThe user reviews the drafts with you. Each turn, first apply the user's "
    "corrections with the tools, checking them against the code, then ask one to three "
    "focused questions with ask_user about what is least certain. When the user says "
    "something the code does not do, tell them: the contract tests run against the code "
    "as it is. Once there are no open problems, tell the user they can finalize the "
    "onboarding."
)

DRAFT_FALLBACK = (
    "I've drafted the contract from the code: {requirements} requirements, {endpoints} "
    "endpoints and {schemas} schemas. Review them and tell me what to correct."
)


ARCHITECTURE_GUIDANCE = (
    "You are a senior engineer adding the top-level architecture to the contract of an "
    "existing service. The service's Git repository is checked out for you; read it with "
    "list_files, search_files and read_file. Paths are relative to the repository root. "
    "The repository already has a contract, given below as its requirements and its API; "
    "they stay exactly as they are, and only contracts/architecture.md and "
    "contracts/architecture.json are added from what you record: the components or modules "
    "of the service with their responsibilities, the data flow between them and the key "
    "technology choices. " + ARCHITECTURE_TOOLS_GUIDANCE + "\n"
    "The architecture describes the code as it is: every component, data flow and "
    "technology choice must be backed by code you read. Compare it with the existing "
    "requirements and API: whenever it disagrees with them (such as a component serving "
    "an endpoint the API does not have, or a technology a requirement rules out), record "
    "the disagreement with record_conflict, and remove it with resolve_conflict once the "
    "architecture agrees; the contract itself cannot change here."
)

ARCHITECTURE_DRAFT_MESSAGE = (
    "Draft the architecture of the service in this repository from its existing code: read "
    "the code first, record its components with their responsibilities, the data flow "
    "between them and the key technology choices, record any disagreement with the "
    "existing contract, then briefly describe the draft and ask your first questions with "
    "ask_user."
)

ARCHITECTURE_FIX_GUIDANCE = (
    "You are a senior engineer maintaining the top-level architecture an onboarding added "
    "to the contract of an existing service, as contracts/architecture.md and "
    "contracts/architecture.json on the branch of its pull request. A reviewer mentioned "
    "you on that pull request to request a fix; their comment follows. Change the "
    "architecture as the comment asks, with the tools, and nothing else: its components "
    "or modules with their responsibilities, the data flow between them and the key "
    "technology choices. " + ARCHITECTURE_TOOLS_GUIDANCE + "\n"
    "The service's Git repository is checked out for you as the pull request holds it; "
    "read it with list_files, search_files and read_file, paths relative to the "
    "repository root. The architecture describes the code as it is: check each change "
    "against the code you read, and do not record what the code contradicts. The "
    "repository's requirements and API, given below, stay exactly as they are. When a "
    "tool call fails, fix the arguments and call it again. End with a short summary of "
    "what you changed, or of why you changed nothing, for the reviewer."
)

ARCHITECTURE_FALLBACK = (
    "I've drafted the architecture from the code: {components} components, {flows} data "
    "flows and {choices} technology choices. Review it and tell me what to correct."
)


def architecture_fallback(structure: Mapping[str, Any] | None) -> ConversationMessage:
    structure = structure or {"components": [], "data_flows": [], "technology_choices": []}
    return {"role": "assistant", "content": ARCHITECTURE_FALLBACK.format(
        components=len(structure["components"]), flows=len(structure["data_flows"]),
        choices=len(structure["technology_choices"]))}


def _conversation(state: OnboardingState, first: str) -> list[BaseMessage]:
    conversation: list[BaseMessage] = [HumanMessage(content=first)]
    conversation += [
        HumanMessage(content=item["content"]) if item["role"] == "user"
        else AIMessage(content=message_text(item))
        for item in state.messages
    ]
    return conversation


def turn_messages(state: OnboardingState, extra_guidance: str = "",
                  contract: Mapping[str, Any] | None = None) -> list[BaseMessage]:
    """The messages of a drafting turn (no messages yet) or of an interview turn; for an
    architecture-only onboarding, `contract` is the existing contract it is checked
    against."""
    if state.architecture_only:
        guidance = (
            f"{ARCHITECTURE_GUIDANCE}{QUESTIONS_GUIDANCE}"
            f"{REVIEW_GUIDANCE if state.drafted else ''}{extra_guidance}"
            f"\n\nExisting requirements: "
            f"{json.dumps((contract or {}).get('requirements', []), ensure_ascii=False)}"
            f"\nExisting API: {json.dumps((contract or {}).get('api', {}), ensure_ascii=False)}"
            f"{CURRENT_STATE_GUIDANCE}"
        )
        return with_current_state(
            [SystemMessage(content=guidance), *_conversation(state, ARCHITECTURE_DRAFT_MESSAGE)],
            f"Current architecture: "
            f"{json.dumps(state.architecture_structure, ensure_ascii=False)}"
            f"\nRecorded conflicts: {json.dumps(state.conflicts, ensure_ascii=False)}"
            f"\nOpen problems: {json.dumps(onboarding_problems(state), ensure_ascii=False)}")
    guidance = (
        f"{GUIDANCE}{QUESTIONS_GUIDANCE}{REVIEW_GUIDANCE if state.drafted else ''}"
        f"{language_guidance(state.language)}{extra_guidance}"
        f"\nSupported languages: {describe_supported()}.{CURRENT_STATE_GUIDANCE}"
    )
    return with_current_state(
        [SystemMessage(content=guidance), *_conversation(state, DRAFT_MESSAGE)],
        f"Current requirements: {json.dumps(state.requirements, ensure_ascii=False)}"
        f"\nCurrent API model: {json.dumps(state.api, ensure_ascii=False)}"
        f"\nCurrent architecture: "
        f"{json.dumps(state.architecture_structure, ensure_ascii=False)}"
        f"\nOpen problems: {json.dumps(onboarding_problems(state), ensure_ascii=False)}")


def draft_fallback(requirements: int, endpoints: int, schemas: int) -> ConversationMessage:
    return {"role": "assistant", "content": DRAFT_FALLBACK.format(
        requirements=requirements, endpoints=endpoints, schemas=schemas)}
