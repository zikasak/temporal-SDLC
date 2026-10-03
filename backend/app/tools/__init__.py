"""Tools the model can call. To add one, create a module with
`build(ctx: ToolContext) -> list[BaseTool]` and append it to TOOLSETS."""

from langchain_core.tools import BaseTool

from app.tools import document_read, document_write
from app.tools.context import ToolContext

TOOLSETS = [document_read.build, document_write.build]


def build_tools(ctx: ToolContext) -> list[BaseTool]:
    return [tool for build in TOOLSETS for tool in build(ctx)]
