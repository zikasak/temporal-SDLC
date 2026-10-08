"""Prompts for the API design discussion."""

import json

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.api_design.models import ApiDesignState
from app.api_design.openapi import design_problems
from app.languages import describe_supported

OPENING_FALLBACK = (
    "I've read the requirements document. Let's design the API of the system. "
    "Who will call it (a web frontend, mobile apps, other services), and how "
    "should callers authenticate?"
)

KICKOFF_MESSAGE = (
    "Here is my requirements document. Start the API design: set the API info, "
    "record a first draft of the schemas and endpoints that clearly follow from the "
    "requirements, then briefly describe the draft and ask your first questions."
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
    "three focused questions about the least settled topics: callers and "
    "authentication, resources and their fields, filtering, sorting and "
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
    "turn which language to use, naming the supported ones. Supported languages: "
    "{supported}. Never assume a language. When the user or the repository names an "
    "unsupported one, say that it is not supported yet and ask the user to choose a "
    "supported language instead."
)
LANGUAGE_KNOWN = (
    "\nThe server code will be written in {language}. Change it with set_language "
    "only when the user asks, and only to a supported language: {supported}."
)


def language_guidance(language: str | None) -> str:
    template = LANGUAGE_KNOWN if language else LANGUAGE_UNKNOWN
    return template.format(language=language, supported=describe_supported())


def api_turn_messages(state: ApiDesignState, extra_guidance: str = "") -> list[BaseMessage]:
    problems = design_problems(state.api, state.language)
    guidance = (
        f"{DESIGNER_GUIDANCE}{language_guidance(state.language)}{extra_guidance}\n\n<requirements_document>\n{state.requirements_document}"
        f"\n</requirements_document>\n\nCurrent API model: "
        f"{json.dumps(state.api, ensure_ascii=False)}\nOpen problems: "
        f"{json.dumps(problems, ensure_ascii=False)}"
    )
    # Chat templates expect the conversation to start with a user turn.
    conversation: list[BaseMessage] = [HumanMessage(content=KICKOFF_MESSAGE)]
    conversation += [
        HumanMessage(content=item["content"]) if item["role"] == "user"
        else AIMessage(content=item["content"])
        for item in state.messages
    ]
    return [SystemMessage(content=guidance), *conversation]
