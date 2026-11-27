"""Prompts for the requirements interview and the final document."""

import json

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.conversations.models import ConversationMessage
from app.llm.prompt import CURRENT_STATE_GUIDANCE, with_current_state
from app.llm.questions import Question, message_text
from app.requirements.models import TASK_KINDS, Requirement, TaskKind
from app.tools.questions import GUIDANCE as QUESTIONS_GUIDANCE

OPENING_MESSAGE = ("Hi! I'll help you work out what needs to be done. First, tell me what "
                   "kind of task this is.")
# The greeting a session started from what needs to be done opens with; the kind of task
# is proposed by the interviewer and confirmed by the user in the conversation.
TASK_OPENING_MESSAGE = ("Hi! I'll help you work out what needs to be done. Tell me what "
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


def opening_message() -> ConversationMessage:
    """The first message of an interview started without a state, as sessions were before
    they started from what needs to be done: it asks what kind of task this is."""
    return {"role": "assistant", "content": OPENING_MESSAGE,
            "questions": [{**KIND_QUESTION, "options": list(KIND_QUESTION["options"])}]}


def task_messages(task: str) -> list[ConversationMessage]:
    """The opening of a new session: the greeting, and what needs to be done as the
    user's first message, which the interviewer answers at once, proposing the kind."""
    return [{"role": "assistant", "content": TASK_OPENING_MESSAGE},
            {"role": "user", "content": task.strip()}]


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
    "to the user; they can see them. The user's first message says what needs to be "
    "done: build on it and never ask for it again. Gather everything else you need in "
    "the conversation."
)

KIND_UNKNOWN = (
    "\nThe kind of task is not recorded yet, and the rest of the interview depends "
    "on it. The kinds are: {kinds}. Propose the kind that fits what needs to be done "
    "best and ask the user with ask_user to confirm it, with the kinds as its options "
    "and the proposed one first ({options}). Never record a kind the user has not "
    "confirmed: call set_task_kind only once the user has confirmed your proposal or "
    "named another kind themselves. Until then, ask nothing else."
)

KIND_KNOWN = (
    "\nThe user confirmed this task is {description}. Change it with set_task_kind only "
    "when the user says it is a different kind."
)

# The service may be one project of several in its repository, such as a web frontend
# beside the backend, and a task that changes what the users can do concerns them too.
# A criterion about how a page looks or fits, recorded so that a browser test can decide
# it rather than a person's judgement.
LAYOUT_CRITERIA = (
    " When a criterion is about how a page looks or fits, record it in measurable form: "
    "which element, at which window sizes (such as a desktop window by mouse, or a phone "
    "by tap), by which input, and what must hold (inside the window, on top and not cut "
    "off, before or after another element). \"Looks right\" is not a criterion."
)
PARTS_INTERVIEW = (
    " The task covers the whole project, not only its API: when the project has a user "
    "interface or other parts that use the service, such as a web frontend, also gather "
    "what changes in them (the pages, forms and user actions, and what they show)."
    + LAYOUT_CRITERIA
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
        "\"Generate document\"." + PARTS_INTERVIEW
    ),
    "api_change": (
        " Keep the interview short and gather only what changes: which endpoints or "
        "fields are added, changed or removed, the behaviour expected of each, and "
        "what must stay as it is. Do not re-gather requirements the existing service "
        "already meets. Each turn, ask one to three focused questions. Once the change "
        "is clear, tell the user they can press \"Generate document\"." + PARTS_INTERVIEW
    ),
    "bugfix": (
        " Keep the interview to a short brief with three parts: the problem (what "
        "happens now and how to trigger it), the expected behaviour, and the acceptance "
        "criteria that show it is fixed. Record each part as functional requirements "
        "and do not gather anything about the rest of the system. Each turn, ask one "
        "to three focused questions about the missing parts. Once all three are "
        "recorded, tell the user they can press \"Generate document\"." + LAYOUT_CRITERIA
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


def service_guidance(published: list[Requirement]) -> str:
    """What a change to an existing service is told about the contract it changes."""
    if not published:
        return ""
    return (SERVICE_GUIDANCE + "\nThe service's requirements: "
            + json.dumps(published, ensure_ascii=False))


def kind_guidance(kind: TaskKind | None) -> str:
    if kind is None:
        kinds = "; ".join(f"{name} ({text})" for name, text in TASK_KINDS.items())
        options = "; ".join(KIND_OPTIONS.values())
        return KIND_UNKNOWN.format(kinds=kinds, options=options)
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
                + service_guidance(published or []) + CURRENT_STATE_GUIDANCE)
    state = "Requirements recorded so far: " + json.dumps(requirements, ensure_ascii=False)
    if published:
        state += ("\nRetired by this change so far: "
                  + json.dumps([item["id"] for item in retired or []]))
    return with_current_state([SystemMessage(content=guidance), *_conversation(messages)], state)


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
