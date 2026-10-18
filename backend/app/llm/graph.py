import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.llm.questions import asks_directly, without_questions

logger = logging.getLogger(__name__)
MAX_LOGGED_ERROR_CHARS = 2000
# How often a reply that asks the user something in its text is sent back.
DIRECT_QUESTION_RETRIES = 2
DIRECT_QUESTION_CORRECTION = (
    "[System] Your reply asked the user a question in its text, but the user can only "
    "answer questions asked with the ask_user tool. Write the reply again without any "
    "question in it, and put each question you need answered into one ask_user call."
)


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


@asynccontextmanager
async def native_session(model: Runnable):
    """Keep one provider conversation across the graph's model calls when the model
    offers it, so each call only appends to the prompt the provider has cached."""
    session = getattr(model, "async_session", None)
    if session is None:
        yield
        return
    async with session():
        yield


def get_message_graph(tools: list[BaseTool], until: Callable[[], bool] | None = None,
                      *, model: Runnable):
    """The model ↔ tool loop. `until` ends it after a round of tool calls, such as the
    one that asked the user something; the model then gets no further turn."""
    from app.llm.claude import ChatClaude, ClaudeGraph

    if isinstance(model, ChatClaude):
        return ClaudeGraph(model, tools, until)
    graph = StateGraph(MessagesState)
    bound_model = model.bind_tools(tools)

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
    return graph.compile()


def final_reply(messages: list[BaseMessage]) -> str:
    """The text of the model's last message."""
    last = next((item for item in reversed(messages) if isinstance(item, AIMessage)), None)
    return str(last.text).strip() if last is not None else ""


async def interview(graph: Any, messages: list[BaseMessage], config: dict[str, Any],
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
