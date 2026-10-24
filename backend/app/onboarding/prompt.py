"""Prompts of an onboarding: drafting the contract from the code, then reviewing it."""

import json

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.api_design.prompt import language_guidance
from app.conversations.models import ConversationMessage
from app.languages import describe_supported
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
    "show_api) and the language it is written in (set_language).\n"
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
    "the code first, record the language with set_language, the requirements and the "
    "whole HTTP API it serves, then briefly describe the draft, say what you were unsure "
    "about and ask your first questions with ask_user."
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


def turn_messages(state: OnboardingState, extra_guidance: str = "") -> list[BaseMessage]:
    """The messages of a drafting turn (no messages yet) or of an interview turn."""
    guidance = (
        f"{GUIDANCE}{QUESTIONS_GUIDANCE}{REVIEW_GUIDANCE if state.drafted else ''}"
        f"{language_guidance(state.language)}{extra_guidance}"
        f"\nSupported languages: {describe_supported()}."
        f"\n\nCurrent requirements: {json.dumps(state.requirements, ensure_ascii=False)}"
        f"\nCurrent API model: {json.dumps(state.api, ensure_ascii=False)}"
        f"\nOpen problems: {json.dumps(onboarding_problems(state), ensure_ascii=False)}"
    )
    conversation: list[BaseMessage] = [HumanMessage(content=DRAFT_MESSAGE)]
    conversation += [
        HumanMessage(content=item["content"]) if item["role"] == "user"
        else AIMessage(content=message_text(item))
        for item in state.messages
    ]
    return [SystemMessage(content=guidance), *conversation]


def draft_fallback(requirements: int, endpoints: int, schemas: int) -> ConversationMessage:
    return {"role": "assistant", "content": DRAFT_FALLBACK.format(
        requirements=requirements, endpoints=endpoints, schemas=schemas)}
