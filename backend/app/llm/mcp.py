"""Expose the current flow's executable tools to native Claude sessions over MCP."""
import json
from collections.abc import Callable, Sequence

from claude_agent_sdk import create_sdk_mcp_server, tool
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

TOOL_SERVER = 'flow'
TOOL_PREFIX = f'mcp__{TOOL_SERVER}__'


def flow_server(tools: Sequence[BaseTool], *, until: Callable[[], bool] | None = None,
                notices: Callable[[], list[str]] | None = None):
    """`notices` gives what the model should read next (each taken once); it follows the
    next tool result."""

    def with_notices(text: str) -> str:
        taken = notices() if notices is not None else []
        return '\n\n'.join([text, *taken])

    def expose(item):
        schema = convert_to_openai_tool(item)['function']

        @tool(item.name, item.description, schema['parameters'])
        async def execute(arguments):
            if until is not None and until():
                return {'content': [{'type': 'text', 'text': 'The flow is paused for the user.'}],
                        'is_error': True}
            try:
                # The Claude plugin owns tool observations, including errors. Do not
                # also export LangChain callbacks for this same tool execution.
                result = await item.ainvoke(arguments, config={'callbacks': []})
                text = result if isinstance(result, str) else json.dumps(result, default=str)
                return {'content': [{'type': 'text', 'text': with_notices(text)}]}
            except Exception as exc:
                return {'content': [{'type': 'text', 'text': with_notices(
                                         f'Tool failed: {type(exc).__name__}: {exc}')}],
                        'is_error': True}

        return execute

    return create_sdk_mcp_server(TOOL_SERVER, tools=[expose(item) for item in tools])
