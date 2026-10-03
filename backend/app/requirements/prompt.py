"""Prompts for the requirements interview and the final document."""

import json

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.conversations.models import ConversationMessage
from app.requirements.models import Requirement

OPENING_MESSAGE = (
    "Hi! I'll help you work out the requirements for your system. "
    "Tell me about what you want to build: what problem it solves and who will use it."
)

INTERVIEWER_GUIDANCE = (
    "You are a business analyst interviewing a user to gather requirements for a "
    "software system. Cover three categories:\n"
    "- functional: what the system must do (features, user roles, workflows, data "
    "the users manage, business rules);\n"
    "- non_functional: performance, scalability, availability, security, privacy and "
    "compliance, usability and accessibility, observability;\n"
    "- technical: technology stack, integrations and external systems, data storage "
    "and migration, deployment and hosting, and other constraints.\n"
    "Each turn, ask one to three focused questions about the least covered topics. "
    "Whenever the user states or confirms a requirement, call record_requirement "
    "right away; pass the existing id to refine a requirement instead of duplicating "
    "it, and call remove_requirement when the user rejects one. Write each "
    "description as one clear, testable statement and only record what the user "
    "actually said or confirmed. Choose the priority from the user's wording: must "
    "for anything mandatory or required, could for nice-to-haves, otherwise should. "
    "When you infer a likely requirement, ask the user to confirm it before "
    "recording it. Do not list the recorded requirements back "
    "to the user; they can see them. Once all three categories are reasonably "
    "covered, tell the user they can press \"Generate document\"."
)

DOCUMENT_GUIDANCE = (
    "You write the introductory and closing parts of a software requirements "
    "document from an interview transcript and the list of agreed requirements. "
    "Reply with Markdown containing exactly these three level-two sections and "
    "nothing else:\n"
    "## Overview\nThe purpose of the system, its users, and its scope.\n"
    "## Assumptions\nAssumptions made during the interview, as a bullet list.\n"
    "## Open questions\nTopics the interview did not settle, as a bullet list.\n"
    "Do not restate the individual requirements and do not invent facts."
)


def _conversation(messages: list[ConversationMessage]) -> list[BaseMessage]:
    return [
        HumanMessage(content=item["content"]) if item["role"] == "user"
        else AIMessage(content=item["content"])
        for item in messages
    ]


def interview_messages(
    messages: list[ConversationMessage], requirements: list[Requirement]
) -> list[BaseMessage]:
    guidance = INTERVIEWER_GUIDANCE + "\nRequirements recorded so far: " + json.dumps(
        requirements, ensure_ascii=False
    )
    return [SystemMessage(content=guidance), *_conversation(messages)]


def document_messages(
    messages: list[ConversationMessage], requirements: list[Requirement]
) -> list[BaseMessage]:
    transcript = "\n\n".join(
        f"{'User' if item['role'] == 'user' else 'Analyst'}: {item['content']}"
        for item in messages
    )
    return [
        SystemMessage(content=DOCUMENT_GUIDANCE),
        HumanMessage(content=(
            f"<transcript>\n{transcript}\n</transcript>\n\n<requirements>\n"
            f"{json.dumps(requirements, ensure_ascii=False)}\n</requirements>"
        )),
    ]
