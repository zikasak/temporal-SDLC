"""Prompts of the architecture session: drafting the task's top-level architecture from
its requirements, and reviewing it with the user."""

import json
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.architecture.models import ArchitectureState, architecture_problems
from app.conversations.models import ConversationMessage
from app.llm.prompt import CURRENT_STATE_GUIDANCE, with_current_state
from app.llm.questions import message_text
from app.tools.questions import GUIDANCE as QUESTIONS_GUIDANCE

TOOLS_GUIDANCE = (
    "You record the architecture with tools: record_component (a component or module and "
    "its responsibilities), record_data_flow (what one component passes to another and "
    "how), record_technology_choice (a key technology choice with its rationale), "
    "remove_component, remove_data_flow, remove_technology_choice and show_architecture. "
    "Every component needs responsibilities, every data flow connects two recorded "
    "components and says what data flows, and the key technology choices (such as the "
    "language and framework, storage, messaging and external services) are recorded. "
    "When a tool call fails, fix the arguments and call it again; only report changes "
    "whose tool call succeeded. Do not paste the architecture back to the user; they can "
    "see it."
)

ARCHITECT_GUIDANCE = (
    "You are a senior software architect. From the requirements document below you "
    "prepare the top-level architecture of the task: its components or modules, their "
    "responsibilities, the data flow between them and the key technology choices. Keep it "
    "at the top level: no endpoints, schemas or field lists, which the API design that "
    "follows settles. " + TOOLS_GUIDANCE + "\n"
    "Each turn, first apply what the user decided with the tools, then ask one to three "
    "focused questions with ask_user about the least settled topics. Record your own "
    "proposals only when they follow directly from the requirements, and tell the user "
    "what you assumed. Once there are no open problems, tell the user they can press "
    "\"Finalize architecture\"."
)

CHANGE_GUIDANCE = (
    "\nThis task changes an existing service. The architecture below started as the "
    "repository's existing contracts/architecture.md; change only what the change document "
    "needs and keep everything else as it is."
)

SKIP_GUIDANCE = (
    "\nBefore drafting, judge whether this API change needs any new architecture at all. "
    "When it clearly does not (for example a new field or endpoint inside an existing "
    "component), call propose_skip with the reason instead of recording anything: the "
    "user then confirms or declines skipping the stage, and the existing architecture "
    "stays as it is. Otherwise draft the change of the architecture."
)

DECLINED_GUIDANCE = (
    "\nThe user declined skipping the architecture stage. Never propose skipping again; "
    "draft the architecture from what the user says it should cover."
)

KICKOFF_MESSAGE = (
    "Here is my requirements document. Draft the architecture: record the components with "
    "their responsibilities, the data flow between them and the key technology choices "
    "that clearly follow from the requirements, then briefly describe the draft and ask "
    "your first questions with ask_user."
)

CHANGE_KICKOFF_MESSAGE = (
    "Here are the requirements document of my existing service, the document of the change "
    "I want and the service's existing architecture. Record what the change needs in the "
    "architecture, then briefly say what you changed and ask your first questions with "
    "ask_user."
)

OPENING_FALLBACK = (
    "I've read the requirements. Let's prepare the architecture, starting with the "
    "components the system consists of and what each one is responsible for."
)

DRAFT_FALLBACK = (
    "I've drafted the architecture: {components} components, {flows} data flows and "
    "{choices} technology choices. Review it and tell me what to change."
)


def opening_fallback() -> ConversationMessage:
    """The first message of a session whose drafting turn failed for good."""
    return {"role": "assistant", "content": OPENING_FALLBACK}


def draft_fallback(structure: Mapping[str, Any]) -> str:
    return DRAFT_FALLBACK.format(components=len(structure["components"]),
                                 flows=len(structure["data_flows"]),
                                 choices=len(structure["technology_choices"]))


def turn_messages(state: ArchitectureState, may_skip: bool,
                  extra_guidance: str = "") -> list[BaseMessage]:
    change = bool(state.change_document)
    current = state.structure if state.structure is not None else state.existing_structure
    guidance = (
        f"{ARCHITECT_GUIDANCE}{QUESTIONS_GUIDANCE}{CHANGE_GUIDANCE if change else ''}"
        f"{SKIP_GUIDANCE if may_skip else ''}"
        f"{DECLINED_GUIDANCE if state.skip_declined else ''}{extra_guidance}"
        f"\n\n<requirements_document>\n{state.requirements_document}\n</requirements_document>"
        + (f"\n\n<change_document>\n{state.change_document}\n</change_document>"
           if change else "")
        + (f"\n\n<existing_architecture>\n{state.existing_architecture}\n"
           "</existing_architecture>" if state.existing_architecture else "")
        + CURRENT_STATE_GUIDANCE
    )
    conversation: list[BaseMessage] = [
        HumanMessage(content=CHANGE_KICKOFF_MESSAGE if change else KICKOFF_MESSAGE)]
    conversation += [
        HumanMessage(content=item["content"]) if item["role"] == "user"
        else AIMessage(content=message_text(item))
        for item in state.messages
    ]
    return with_current_state(
        [SystemMessage(content=guidance), *conversation],
        f"Current architecture: {json.dumps(current, ensure_ascii=False)}"
        f"\nOpen problems: {json.dumps(architecture_problems(current), ensure_ascii=False)}")
