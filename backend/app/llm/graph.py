import json
import logging
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Protocol, cast

from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, messages_from_dict,
    messages_to_dict,
)
from langchain_core.tools import BaseTool
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.asyncio_utils import run_sync
from app.llm.questions import asks_directly, without_questions
from app.llm import saved_conversations

logger = logging.getLogger(__name__)
MAX_LOGGED_ERROR_CHARS = 2000
# How often a reply that asks the user something in its text is sent back.
DIRECT_QUESTION_RETRIES = 2
DIRECT_QUESTION_CORRECTION = (
    "[System] Your reply asked the user a question in its text, but the user can only "
    "answer questions asked with the ask_user tool. Write the reply again without any "
    "question in it, and put each question you need answered into one ask_user call."
)
# A saved conversation larger than this starts anew from the application's messages, as
# the providers outside Claude do not compact their history.
MAX_RESTORED_BYTES = 400_000
# The saved conversations of different providers stay apart: their messages carry
# provider-specific details.
PROVIDER_NAMES = {"ChatOpenAI": "openai", "ChatGoogleGenerativeAI": "gemini", "ChatPi": "pi"}


class DirectQuestion(ValueError):
    """The model kept asking the user in its reply text instead of with ask_user."""


async def _log_tool_call(request, execute):
    name = request.tool_call["name"]
    logger.debug("Tool call %s with %s", name, request.tool_call["args"])
    result = await execute(request)
    # ToolNode turns invalid arguments into an error message for the model.
    if isinstance(result, ToolMessage) and result.status == "error":
        logger.warning("Tool %s failed: %s", name, str(result.content)[:MAX_LOGGED_ERROR_CHARS])
    else:
        logger.info("Tool call %s", name)
    return result


class _BindsTools(Protocol):
    """A chat model that can call tools; models are duck-typed, so tests can fake one."""

    def bind_tools(self, tools: Sequence[BaseTool]) -> Runnable: ...


class _SavedConversation:
    """The provider messages of a conversation turn, saved for the next turn: its tool
    calls and results, which the application's own messages leave out, and the exact
    prompt the provider has cached."""

    def __init__(self, store: Any, conversation: saved_conversations.ConversationRef,
                 provider: str):
        self.store, self.conversation = store, conversation
        self.name = f"{conversation.name}/{provider}.json"
        self.source = f"{conversation.source}/{provider}.json"
        self.history: list[BaseMessage] | None = None
        self.result: list[BaseMessage] | None = None

    async def prompt(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        """The turn's first prompt continues the saved messages with the new ones; later
        ones (corrections) already continue the turn's own."""
        if self.history is not None:
            return messages
        system = [message for message in messages if isinstance(message, SystemMessage)]
        self.history = [message for message in messages
                        if not isinstance(message, SystemMessage)]
        restored: list[BaseMessage] = []
        consumed = 0
        try:
            data = await run_sync(self.store.load, self.source)
            if data is not None and len(data) <= MAX_RESTORED_BYTES:
                saved = json.loads(data)
                consumed = saved_conversations.continues(
                    saved.get("meta"), self.history, self.conversation.context)
                if consumed:
                    restored = messages_from_dict(saved["messages"])
        except Exception:
            logger.warning("Could not restore the conversation %s", self.source,
                           exc_info=True)
            restored, consumed = [], 0
        if not consumed:
            return messages
        logger.info("Continuing the saved conversation %s after %d messages",
                    self.source, consumed)
        return [*system, *restored, *self.history[consumed:]]

    def record(self, messages: list[BaseMessage]) -> None:
        self.result = [message for message in messages
                       if not isinstance(message, SystemMessage)]

    async def save(self) -> None:
        if self.history is None or self.result is None:
            return
        try:
            data = json.dumps({"meta": saved_conversations.saved_meta(
                                   self.history, self.conversation.context),
                               "messages": messages_to_dict(self.result)},
                              ensure_ascii=False).encode()
            await run_sync(self.store.save, self.name, data)
        except Exception:
            logger.warning("Could not save the conversation %s", self.name, exc_info=True)
            return
        logger.info("Saved the conversation %s (%d bytes)", self.name, len(data))


# The saved conversation of the turn native_session runs, which the graph continues.
_CONVERSATION: ContextVar[_SavedConversation | None] = ContextVar(
    "saved_conversation", default=None)


@asynccontextmanager
async def native_session(model: Runnable,
                         conversation: saved_conversations.ConversationRef | None = None):
    """Keep one provider conversation across the graph's model calls when the model
    offers it, so each call only appends to the prompt the provider has cached.
    `conversation` names the application conversation this turn belongs to: its provider
    conversation (Claude's native session, other providers' messages with their tool
    calls) is restored from the shared storage and saved again after a successful turn."""
    session = getattr(model, "async_session", None)
    if session is not None:
        if conversation is not None and hasattr(model, "conversation"):
            setattr(model, "conversation", conversation)
        async with session():
            yield
        return
    store = saved_conversations.get_store() if conversation is not None else None
    if store is None:
        yield
        return
    provider = PROVIDER_NAMES.get(type(model).__name__, type(model).__name__.lower())
    assert conversation is not None
    saved = _SavedConversation(store, conversation, provider)
    token = _CONVERSATION.set(saved)
    try:
        yield
    finally:
        _CONVERSATION.reset(token)
    await saved.save()


class _ConversationGraph:
    """The compiled graph, continuing the saved conversation of the running turn."""

    def __init__(self, graph: Any):
        self.graph = graph

    async def ainvoke(self, state: dict[str, Any], config: RunnableConfig | None = None,
                      **kwargs: Any) -> Any:
        saved = _CONVERSATION.get()
        if saved is None:
            return await self.graph.ainvoke(state, config, **kwargs)
        result = await self.graph.ainvoke(
            {**state, "messages": await saved.prompt(state["messages"])}, config, **kwargs)
        saved.record(result["messages"])
        return result


def get_message_graph(tools: list[BaseTool], until: Callable[[], bool] | None = None,
                      *, model: Runnable) -> Any:
    """The model ↔ tool loop. `until` ends it after a round of tool calls, such as the
    one that asked the user something; the model then gets no further turn."""
    from app.llm.claude import ChatClaude, ClaudeGraph

    if isinstance(model, ChatClaude):
        return ClaudeGraph(model, tools, until)
    graph = StateGraph(MessagesState)
    bound_model = cast(_BindsTools, model).bind_tools(tools)

    async def call_model(state: MessagesState):
        return {"messages": [await bound_model.ainvoke(
            state["messages"], config={"run_name": "decide-next-action"},
        )]}

    graph.add_node("call_model", call_model)
    graph.add_node("tools", ToolNode(tools, awrap_tool_call=_log_tool_call))
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges("call_model", tools_condition)
    if until is None:
        graph.add_edge("tools", "call_model")
    else:
        graph.add_conditional_edges("tools", lambda state: END if until() else "call_model",
                                    ["call_model", END])
    return _ConversationGraph(graph.compile())


def final_reply(messages: list[BaseMessage]) -> str:
    """The text of the model's last message."""
    last = next((item for item in reversed(messages) if isinstance(item, AIMessage)), None)
    return str(last.text).strip() if last is not None else ""


async def interview(graph: Any, messages: list[BaseMessage], config: RunnableConfig,
                    asked: Callable[[], bool]) -> str:
    """Run an interviewing turn and return its reply. The user answers only questions
    asked with ask_user (`asked` tells whether the turn did), so a reply that asks in its
    own text is sent back; one that keeps doing so raises DirectQuestion. A reply that
    comes with ask_user only loses the questions it repeats."""
    for attempt in range(DIRECT_QUESTION_RETRIES + 1):
        result = await graph.ainvoke({"messages": messages}, config=config)
        messages = result["messages"]
        reply = final_reply(messages)
        if asked():
            return without_questions(reply)
        if not asks_directly(reply):
            return reply
        logger.warning("Reply asked the user directly (attempt %d)", attempt + 1)
        messages = [*messages, HumanMessage(content=DIRECT_QUESTION_CORRECTION)]
    raise DirectQuestion("The model asked the user in its reply instead of with ask_user")
