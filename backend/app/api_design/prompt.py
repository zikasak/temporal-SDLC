"""Prompts for the API design discussion."""

import json

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.api_design.models import ApiDesignState
from app.api_design.openapi import design_problems
from app.conversations.models import ConversationMessage
from app.languages import describe_supported
from app.llm.questions import message_text
from app.tools.questions import GUIDANCE as QUESTIONS_GUIDANCE

OPENING_FALLBACK = (
    "I've read the requirements document. Let's design the API of the system, starting "
    "with who calls it and how."
)
OPENING_QUESTIONS = [
    {"question": "Who will call the API?",
     "options": ["A web frontend", "Mobile apps", "Other services"]},
    {"question": "How should callers authenticate?",
     "options": ["No authentication", "Bearer tokens", "API keys", "HTTP basic authentication"]},
]

KICKOFF_MESSAGE = (
    "Here is my requirements document. Start the API design: set the API info, "
    "record a first draft of the schemas and endpoints that clearly follow from the "
    "requirements, then briefly describe the draft and ask your first questions with "
    "ask_user."
)

CHANGE_OPENING_FALLBACK = (
    "I've read the change you want and the API your service has now. Tell me which "
    "endpoint or field of the existing API should change first, and how."
)

CHANGE_KICKOFF_MESSAGE = (
    "Here are the requirements document of my existing service and the document of "
    "the change I want. The API model already holds the service's API as it is. "
    "Record what clearly follows from the change, then briefly say what you changed "
    "and ask your first questions with ask_user."
)

DESIGNER_GUIDANCE = (
    "You are a senior API designer. You discuss with the user the HTTP JSON API of "
    "the system described in the requirements document below, and you build its "
    "OpenAPI schema with tools: set_api_info (title, version, description, "
    "authentication), set_language (the implementation language), record_schema "
    "(JSON data models), record_endpoint (operations), remove_schema, "
    "remove_endpoint and show_api.\n"
    "Design RESTful resources: plural nouns in paths, path parameters in braces "
    "such as /rooms/{room_id}, GET to read, POST to create, PUT or PATCH to update, "
    "DELETE to remove. Use separate input schemas (such as BookingCreate) when "
    "clients must not send server-set fields like ids or timestamps. Record every "
    "schema that an endpoint or another schema refers to. Pass requirement_ids to "
    "link each endpoint to the requirements it serves.\n"
    "Each turn, first apply what the user decided with the tools, then ask one to "
    "three focused questions with ask_user about the least settled topics: callers "
    "and authentication, resources and their fields, filtering, sorting and "
    "pagination, error responses, and requirements no endpoint covers yet. Record "
    "your own proposals only when they follow directly from the requirements, and "
    "tell the user what you assumed. When a tool call fails, fix the arguments and "
    "call it again; only report changes whose tool call succeeded. Do not paste the "
    "API back to the user; they can "
    "see it. Fix the open problems listed below. Once the requirements are covered "
    "and there are no problems, tell the user they can press \"Generate OpenAPI\"."
)


LANGUAGE_UNKNOWN = (
    "\nThe language the server code will be written in is not chosen yet, and "
    "nothing can be generated without it. If the project's repository is available "
    "to you and its files clearly show the language it is written in, record that "
    "language with set_language and tell the user; otherwise ask the user in this "
    "turn with ask_user which language to use, with the supported ones as its "
    "options. Supported languages: {supported}. Never assume a language. When the "
    "user or the repository names an unsupported one, say that it is not supported "
    "yet and ask the user to choose a supported language instead."
)
LANGUAGE_KNOWN = (
    "\nThe server code will be written in {language}. Change it with set_language "
    "only when the user asks, and only to a supported language: {supported}."
)


LANGUAGE_FIXED = (
    "\nThe service is written in {language}. A change to it cannot choose another "
    "language; say so if the user asks for one."
)

CHANGE_GUIDANCE = (
    "\nThis discussion changes an existing service; it does not design a new API. "
    "The API model below started as the API the service has now, and the "
    "requirements document is the one the service was built from, with the "
    "requirements of the change added to its tables. The change document below is "
    "what the user asked for, and it alone says what to change. Where it conflicts "
    "with an earlier requirement, the change wins. Add, change or remove only the "
    "endpoints, schemas and fields the change needs, and leave everything else "
    "exactly as it is, down to names, paths and status codes: the service's clients "
    "and its existing tests depend on them. Link each endpoint you add or change to "
    "the requirements of the change. Ask only about the change, and tell the user "
    "when what they ask for would break something existing clients rely on."
)


ARCHITECTURE_GUIDANCE = (
    "The task's top-level architecture below was settled before this discussion. Design "
    "the API so that it fits it: its components, the data flow between them and the "
    "technology choices it records."
)


def opening_fallback(change: bool) -> ConversationMessage:
    """The first message of a session whose first turn failed. Change sessions only
    get theirs when replaying histories from before their opening could be retried."""
    if change:
        return {"role": "assistant", "content": CHANGE_OPENING_FALLBACK}
    return {"role": "assistant", "content": OPENING_FALLBACK,
            "questions": [{**item, "options": list(item["options"])}
                          for item in OPENING_QUESTIONS]}


def language_guidance(language: str | None, fixed: bool = False) -> str:
    template = LANGUAGE_FIXED if fixed else LANGUAGE_KNOWN if language else LANGUAGE_UNKNOWN
    return template.format(language=language, supported=describe_supported())


def api_turn_messages(state: ApiDesignState, extra_guidance: str = "") -> list[BaseMessage]:
    problems = design_problems(state.api, state.language)
    change = bool(state.change_document)
    guidance = (
        f"{DESIGNER_GUIDANCE}{QUESTIONS_GUIDANCE}{CHANGE_GUIDANCE if change else ''}"
        f"{language_guidance(state.language, fixed=change)}{extra_guidance}"
        f"\n\n<requirements_document>\n{state.requirements_document}"
        f"\n</requirements_document>\n\n"
        + (f"<change_document>\n{state.change_document}\n</change_document>\n\n"
           if change else "")
        + (f"{ARCHITECTURE_GUIDANCE}\n<architecture>\n{state.architecture}\n"
           "</architecture>\n\n" if state.architecture else "")
        + f"Current API model: "
        f"{json.dumps(state.api, ensure_ascii=False)}\nOpen problems: "
        f"{json.dumps(problems, ensure_ascii=False)}"
    )
    # Chat templates expect the conversation to start with a user turn.
    conversation: list[BaseMessage] = [
        HumanMessage(content=CHANGE_KICKOFF_MESSAGE if change else KICKOFF_MESSAGE)]
    conversation += [
        HumanMessage(content=item["content"]) if item["role"] == "user"
        else AIMessage(content=message_text(item))
        for item in state.messages
    ]
    return [SystemMessage(content=guidance), *conversation]
