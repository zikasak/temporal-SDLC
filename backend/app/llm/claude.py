"""Native Claude Agent SDK sessions; Claude owns its history and MCP tool loop."""
import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ClaudeSDKError,
    HookMatcher, ResultMessage, TextBlock,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from app.llm.mcp import TOOL_PREFIX, TOOL_SERVER, flow_server
from app.tracing import claude_trace_environment

PLUGIN = Path(__file__).with_name('plugins') / 'langfuse-observability'


class ClaudeTurnLimit(RuntimeError):
    """The native agent used the allowed number of model turns."""


@dataclass
class ChatClaude(Runnable):
    model: str | None = None
    thinking_level: str = 'default'
    cli_path: str | None = None
    timeout: float = 600
    # Coding stages keep native transcripts beside the project through a pause.
    cwd: Path | None = None
    config_dir: Path | None = None
    resume: str | None = None
    session_id: str | None = field(default=None, init=False)
    turns_used: int = field(default=0, init=False)
    _stack: AsyncExitStack | None = field(default=None, init=False, repr=False)
    _client: ClaudeSDKClient | None = field(default=None, init=False, repr=False)
    _messages: list[BaseMessage] = field(default_factory=list, init=False, repr=False)

    @asynccontextmanager
    async def async_session(self):
        if self._stack is not None:
            raise RuntimeError('Claude session is already open')
        async with AsyncExitStack() as stack:
            self._stack = stack
            self._messages = []
            self.turns_used = 0
            try:
                yield self
            finally:
                # The SDK and its anyio task group enter and exit on the caller task.
                self._client = None
                self._stack = None

    def bind_tools(self, tools: Sequence[BaseTool], **kwargs):
        return self.bind(tools=list(tools))

    def invoke(self, input, config=None, **kwargs):
        return asyncio.run(self.ainvoke(input, config, **kwargs))

    async def ainvoke(self, input, config=None, *, tools=(), until=None,
                      max_turns=20, **kwargs) -> AIMessage:
        messages = [HumanMessage(input)] if isinstance(input, str) else list(input)
        if not any(not isinstance(message, SystemMessage) for message in messages):
            raise ValueError('Claude needs at least one non-system message')
        if self._stack is None:
            async with self.async_session():
                return await self._turn(messages, tools, until, max_turns)
        return await self._turn(messages, tools, until, max_turns)

    async def _turn(self, messages, tools, until, max_turns):
        async def stop_after_tools(data, tool_use_id, context):
            if until is not None and until():
                return {'continue': False, 'stopReason': 'Paused for the user'}
            return {}

        if self._client is None:
            cwd = self.cwd
            if cwd is None:
                cwd = Path(self._stack.enter_context(
                    tempfile.TemporaryDirectory(prefix='claude-native-')))
            env = claude_trace_environment()
            env.update(APP_CLAUDE_HOOK_PYTHON=sys.executable,
                       PYTHONPATH=os.pathsep.join(filter(None, [str(Path(__file__).parents[2]),
                                                               os.getenv('PYTHONPATH')])))
            env['CC_LANGFUSE_STATE_DIR'] = str((self.config_dir or cwd) / '.langfuse-state')
            if self.config_dir is not None:
                self.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                env['CLAUDE_CONFIG_DIR'] = str(self.config_dir)
            thinking = ({} if self.thinking_level == 'default' else
                        {'thinking': {'type': 'disabled'}} if self.thinking_level == 'off' else
                        {'thinking': {'type': 'adaptive'}, 'effort': self.thinking_level})
            options = ClaudeAgentOptions(
                model=self.model, cli_path=self.cli_path, cwd=str(cwd),
                system_prompt='\n\n'.join(message.text for message in messages
                                          if isinstance(message, SystemMessage)),
                **thinking, max_turns=max_turns, tools=[], skills=[], setting_sources=[],
                strict_mcp_config=True, verbatim_prompts=True,
                mcp_servers=({TOOL_SERVER: flow_server(tools, until=until)} if tools else {}),
                allowed_tools=[TOOL_PREFIX + item.name for item in tools],
                hooks=({'PostToolUse': [HookMatcher(matcher=TOOL_PREFIX + '.*',
                                                  hooks=[stop_after_tools])]}
                       if until else None),
                plugins=[{'type': 'local', 'path': str(PLUGIN)}] if env.get(
                    'APP_CLAUDE_TRACING_ENABLED') == 'true' else [],
                env=env, resume=self.resume,
            )
            self._client = await self._stack.enter_async_context(ClaudeSDKClient(options))
        # Follow-ups append only new user messages. Native history, thinking, tool
        # results and compaction stay inside Claude; no transcript reconstruction.
        if self._messages:
            if messages[:len(self._messages)] != self._messages:
                raise ValueError('A native Claude session only accepts appended messages')
            added = messages[len(self._messages):]
            prompt = '\n\n'.join(message.text for message in added
                                 if not isinstance(message, SystemMessage))
        else:
            prompt = _prompt(messages)
        result = None
        blocks = []
        message_id = None
        model_name = self.model
        try:
            async with asyncio.timeout(self.timeout):
                await self._client.query(prompt)
                async for event in self._client.receive_response():
                    if isinstance(event, AssistantMessage) and event.parent_tool_use_id is None:
                        if event.message_id != message_id:
                            blocks = []
                        blocks.extend(event.content)
                        message_id = event.message_id
                        model_name = event.model
                    elif isinstance(event, ResultMessage):
                        result = event
        except TimeoutError as exc:
            await self._client.interrupt()
            raise TimeoutError(f'Claude did not finish within {self.timeout:g} seconds') from exc
        except ClaudeSDKError:
            if result is None:
                raise
        if result is None:
            raise RuntimeError('Claude produced no result')
        self.session_id = result.session_id
        self.turns_used += result.num_turns
        paused = until is not None and until()
        if result.is_error and not paused:
            if result.subtype == 'error_max_turns':
                raise ClaudeTurnLimit('Claude exceeded the tool-turn limit')
            raise RuntimeError('Claude response failed: ' + (
                '; '.join(result.errors or []) or result.result or result.subtype))
        reply = AIMessage(
            content=''.join(block.text for block in blocks if isinstance(block, TextBlock))
                    or result.result or '',
            response_metadata={'model_name': model_name, 'session_id': result.session_id,
                               'total_cost_usd': result.total_cost_usd},
        )
        self._messages = [*messages, reply]
        return reply


def _prompt(messages: list[BaseMessage]) -> str:
    history = [message for message in messages if not isinstance(message, SystemMessage)]
    if len(history) == 1 and isinstance(history[0], HumanMessage):
        return history[0].text
    # Application conversations are context for a new native session, never forged
    # Claude transcript rows. Subsequent requests use the native session itself.
    return 'Conversation context:\n' + json.dumps([
        {'role': message.type, 'content': message.content} for message in history
    ], ensure_ascii=False) + '\n\nContinue the conversation and complete the latest user request.'


class ClaudeGraph:
    """The flow's invocation interface, with the SDK owning all model/tool turns."""

    def __init__(self, model: ChatClaude, tools, until):
        self.model, self.tools, self.until = model, tools, until

    async def ainvoke(self, state, config=None):
        try:
            reply = await self.model.ainvoke(
                state['messages'], tools=self.tools, until=self.until,
                max_turns=max(1, (config or {}).get('recursion_limit', 20)),
            )
        except ClaudeTurnLimit as exc:
            from langgraph.errors import GraphRecursionError
            raise GraphRecursionError(str(exc)) from exc
        return {'messages': [*state['messages'], reply]}
