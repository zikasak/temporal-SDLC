"""Tool failures keep their error status through the SDK's MCP conversion."""
import anyio
import pytest
from langchain_core.tools import tool
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import TextContent

from app.llm.mcp import flow_server


@pytest.mark.parametrize('failure', ['missing', 'paused'])
async def test_flow_tool_errors_reach_the_mcp_client(tmp_path, failure):
    paused = False

    @tool
    async def read_file(name: str) -> str:
        """Read a file."""
        return (tmp_path / name).read_text()

    (tmp_path / 'present').write_text('contents')
    server = flow_server([read_file], until=lambda: paused)['instance']
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(server.run, *server_streams, server.create_initialization_options())
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                success = await client.call_tool('read_file', {'name': 'present'})
                assert not success.model_dump(by_alias=True)['isError']
                assert isinstance(success.content[0], TextContent)
                assert success.content[0].text == 'contents'
                paused = failure == 'paused'
                error = await client.call_tool('read_file', {
                    'name': 'present' if paused else 'missing',
                })
                assert error.model_dump(by_alias=True)['isError']
                assert isinstance(error.content[0], TextContent)
                assert ('paused' if paused else 'FileNotFoundError') in error.content[0].text
            tasks.cancel_scope.cancel()


async def test_a_notice_for_claude_follows_the_next_tool_result_once():
    notices = ['Time is nearly up.']

    @tool
    async def look() -> str:
        """Look around."""
        return 'nothing here'

    def take():
        taken = notices[:]
        notices.clear()
        return taken

    server = flow_server([look], notices=take)['instance']
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(server.run, *server_streams, server.create_initialization_options())
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                first = await client.call_tool('look', {})
                second = await client.call_tool('look', {})
                assert isinstance(first.content[0], TextContent)
                assert isinstance(second.content[0], TextContent)
                assert first.content[0].text == 'nothing here\n\nTime is nearly up.'
                assert second.content[0].text == 'nothing here'
            tasks.cancel_scope.cancel()
