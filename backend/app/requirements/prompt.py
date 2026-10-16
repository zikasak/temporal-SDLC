"""Prompts for the requirements interview and the final document."""

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.conversations.models import ConversationMessage
from app.llm.questions import Question, answers_text, message_text
from app.requirements.models import TASK_KINDS, Requirement, TaskKind
from app.tools.questions import GUIDANCE as QUESTIONS_GUIDANCE

OPENING_MESSAGE = ("Hi! I'll help you work out what needs to be done. First, tell me what "
                   "kind of task this is.")
INTAKE_MESSAGE = ("Hi! I'll help you work out what needs to be done. Every task starts with "
                  "the same questions: its kind, a few questions about that kind, and what "
                  "needs to be done.")
# How the kinds are offered to the user; TASK_KINDS describes them to the model.
KIND_OPTIONS: dict[TaskKind, str] = {
    "new_service": "A new service",
    "api_change": "A change to an existing service's API",
    "bugfix": "A bugfix or another change that leaves the API as it is",
    "docs": "A change to documentation only",
}
# The user may still write another kind in the interview.
KIND_QUESTION: Question = {"question": "What kind of task is this?",
                           "options": list(KIND_OPTIONS.values())}
# The questions a new session asks once the kind is chosen, before the interview starts.
INTAKE_QUESTIONS: dict[TaskKind, list[Question]] = {
    "new_service": [
        {"question": "Who will use the service?",
         "options": ["Other services of ours (an internal API)",
                     "External clients or partners (a public API)",
                     "A front end or app of ours"]},
        {"question": "How large is the service?",
         "options": ["A small service with a few endpoints",
                     "A service covering one business area",
                     "A service covering several business areas"]},
    ],
    "api_change": [
        {"question": "Which part of the API does the change touch?",
         "options": ["One existing endpoint", "Several existing endpoints",
                     "A new endpoint or resource",
                     "Fields shared by several endpoints"]},
        {"question": "What is the nature of the change?",
         "options": ["Add new behaviour", "Change existing behaviour",
                     "Remove or deprecate behaviour"]},
        {"question": "Must existing clients keep working unchanged?",
         "options": ["Yes, the change must be backward compatible",
                     "No, breaking changes are acceptable"]},
    ],
    "bugfix": [
        {"question": "Which area of the service is affected?",
         "options": ["Request handling and validation", "Business rules and calculations",
                     "Data storage and retrieval", "Calls to external systems",
                     "Security and access control", "Performance and reliability"]},
        {"question": "What is the nature of the change?",
         "options": ["It returns a wrong result", "It fails with an error",
                     "It accepts input it should reject",
                     "It is not a bug: a refactoring, upgrade or clean-up"]},
    ],
    "docs": [
        {"question": "Which documentation should change?",
         "options": ["The README", "The API reference", "Usage guides and examples",
                     "Code comments"]},
        {"question": "What is the nature of the change?",
         "options": ["Add missing documentation",
                     "Correct wrong or outdated documentation",
                     "Restructure or clarify existing documentation",
                     "Remove obsolete documentation"]},
    ],
}
TASK_QUESTION = "What needs to be done?"


def opening_message() -> ConversationMessage:
    """The first message of an interview that starts without the intake: it asks what
    kind of task this is."""
    return {"role": "assistant", "content": OPENING_MESSAGE,
            "questions": [{**KIND_QUESTION, "options": list(KIND_QUESTION["options"])}]}


def intake() -> dict[str, Any]:
    """The questions every new session starts with, for the page to show up front."""
    return {"kind_question": KIND_QUESTION["question"],
            "kinds": [{"kind": kind, "label": label} for kind, label in KIND_OPTIONS.items()],
            "questions": INTAKE_QUESTIONS, "task_question": TASK_QUESTION}


def intake_messages(kind: TaskKind, answers: list[str], task: str) -> list[ConversationMessage]:
    """The opening of a session started from the intake: the questions it asked, and the
    user's answers to them with the task as the user's first message."""
    questions = [KIND_QUESTION, *INTAKE_QUESTIONS[kind]]
    reply = answers_text(questions, [KIND_OPTIONS[kind], *answers])
    return [
        {"role": "assistant", "content": INTAKE_MESSAGE,
         "questions": [{**item, "options": list(item["options"])} for item in questions]},
        {"role": "user", "content": f"{reply}\n\n{TASK_QUESTION}\n→ {task.strip()}"},
    ]


INTERVIEWER_GUIDANCE = (
    "You are a business analyst interviewing a user to specify a software task. "
    "Whenever the user states or confirms a requirement, call record_requirement "
    "right away; pass the existing id to refine a requirement instead of duplicating "
    "it, and call remove_requirement when the user rejects one. Write each "
    "description as one clear, testable statement and only record what the user "
    "actually said or confirmed. Choose the priority from the user's wording: must "
    "for anything mandatory or required, could for nice-to-haves, otherwise should. "
    "When you infer a likely requirement, ask the user with ask_user to confirm it "
    "before recording it. Do not list the recorded requirements back "
    "to the user; they can see them. The user's first message may answer the starting "
    "questions every task is asked and say what needs to be done: build on those "
    "answers and never ask them again."
)

KIND_UNKNOWN = (
    "\nThe kind of task is not recorded yet, and the rest of the interview depends "
    "on it. The kinds are: {kinds}. Never assume the kind and never infer it from "
    "the conversation or the repository: record it with set_task_kind only when the "
    "user has named it themselves, choosing its option included. If they have not, or "
    "their answer could mean more than one kind, ask them with ask_user in this turn "
    "which it is, with the kinds as its options, and ask nothing else."
)

KIND_KNOWN = (
    "\nThe user said this task is {description}. Change it with set_task_kind only "
    "when the user says it is a different kind."
)

KIND_INTERVIEWS: dict[str, str] = {
    "new_service": (
        " Gather the requirements for the whole system, in three categories:\n"
        "- functional: what the system must do (features, user roles, workflows, data "
        "the users manage, business rules);\n"
        "- non_functional: performance, scalability, availability, security, privacy and "
        "compliance, usability and accessibility, observability;\n"
        "- technical: technology stack, integrations and external systems, data storage "
        "and migration, deployment and hosting, and other constraints.\n"
        "Each turn, ask one to three focused questions about the least covered topics. "
        "Once all three categories are reasonably covered, tell the user they can press "
        "\"Generate document\"."
    ),
    "api_change": (
        " Keep the interview short and gather only what changes: which endpoints or "
        "fields are added, changed or removed, the behaviour expected of each, and "
        "what must stay as it is. Do not re-gather requirements the existing service "
        "already meets. Each turn, ask one to three focused questions. Once the change "
        "is clear, tell the user they can press \"Generate document\"."
    ),
    "bugfix": (
        " Keep the interview to a short brief with three parts: the problem (what "
        "happens now and how to trigger it), the expected behaviour, and the acceptance "
        "criteria that show it is fixed. Record each part as functional requirements "
        "and do not gather anything about the rest of the system. Each turn, ask one "
        "to three focused questions about the missing parts. Once all three are "
        "recorded, tell the user they can press \"Generate document\"."
    ),
    "docs": (
        " Keep the interview to a short brief: which documentation should change, what "
        "it should say afterwards, and how to tell the change is complete. Record "
        "these as requirements and do not gather anything about the service's "
        "behaviour. Each turn, ask one to three focused questions about the missing "
        "parts. Once the brief is clear, tell the user they can press "
        "\"Generate document\"."
    ),
}


SERVICE_GUIDANCE = (
    "\nThe existing service was built to meet the requirements listed below as the "
    "service's requirements. They stay in force and are not part of this interview: "
    "never record one of them again. When the change contradicts, replaces or removes "
    "one of them, that one no longer holds. Say which one it is, and once the user "
    "confirms, call retire_requirement with its ID and record what holds instead, if "
    "anything, as a new requirement. Call keep_requirement to undo this. Before "
    "telling the user they can generate the document, check each of the service's "
    "requirements against the change and settle every conflict this way."
)


def service_guidance(published: list[Requirement], retired: list[Requirement]) -> str:
    """What a change to an existing service is told about the contract it changes."""
    if not published:
        return ""
    return (SERVICE_GUIDANCE + "\nThe service's requirements: "
            + json.dumps(published, ensure_ascii=False)
            + "\nRetired by this change so far: "
            + json.dumps([item["id"] for item in retired]))


def kind_guidance(kind: TaskKind | None) -> str:
    if kind is None:
        kinds = "; ".join(f"{name} ({text})" for name, text in TASK_KINDS.items())
        return KIND_UNKNOWN.format(kinds=kinds)
    return KIND_KNOWN.format(description=TASK_KINDS[kind]) + KIND_INTERVIEWS[kind]


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
        else AIMessage(content=message_text(item))
        for item in messages
    ]


def interview_messages(
    messages: list[ConversationMessage], requirements: list[Requirement],
    kind: TaskKind | None, extra_guidance: str = "",
    published: list[Requirement] | None = None, retired: list[Requirement] | None = None,
) -> list[BaseMessage]:
    """`published` are the requirements of the service a change starts from."""
    guidance = (INTERVIEWER_GUIDANCE + QUESTIONS_GUIDANCE + kind_guidance(kind) + extra_guidance
                + service_guidance(published or [], retired or [])
                + "\nRequirements recorded so far: "
                + json.dumps(requirements, ensure_ascii=False))
    return [SystemMessage(content=guidance), *_conversation(messages)]


def document_messages(
    messages: list[ConversationMessage], requirements: list[Requirement],
    retired: list[Requirement] | None = None,
) -> list[BaseMessage]:
    transcript = "\n\n".join(
        f"{'User' if item['role'] == 'user' else 'Analyst'}: {message_text(item)}"
        for item in messages
    )
    return [
        SystemMessage(content=DOCUMENT_GUIDANCE),
        HumanMessage(content=(
            f"<transcript>\n{transcript}\n</transcript>\n\n<requirements>\n"
            f"{json.dumps(requirements, ensure_ascii=False)}\n</requirements>"
            + (f"\n\n<retired_requirements>\n{json.dumps(retired, ensure_ascii=False)}"
               "\n</retired_requirements>" if retired else "")
        )),
    ]
