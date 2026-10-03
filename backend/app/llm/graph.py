import logging

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.llm.model import get_model

logger = logging.getLogger(__name__)
MAX_LOGGED_ERROR_CHARS = 2000


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


def get_message_graph(tools: list[BaseTool]):
    graph = StateGraph(MessagesState)
    bound_model = get_model().bind_tools(tools)

    async def call_model(state: MessagesState):
        return {"messages": [await bound_model.ainvoke(
            state["messages"], config={"run_name": "decide-next-action"},
        )]}

    graph.add_node("call_model", call_model)
    graph.add_node("tools", ToolNode(tools, awrap_tool_call=_log_tool_call))
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges("call_model", tools_condition)
    graph.add_edge("tools", "call_model")
    return graph.compile()
