"""Native Claude Agent SDK sessions; Claude owns its history and MCP tool loop."""
import asyncio
import json
import logging
import os
import sys
import tempfile
from collections.abc import Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ClaudeSDKError, HookContext,
    HookInput, HookJSONOutput, HookMatcher, ResultMessage,
    SystemMessage as NativeSystemMessage, TextBlock,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from app.asyncio_utils import async_context, run_sync
from app.llm.mcp import TOOL_PREFIX, TOOL_SERVER, flow_server
from app.llm.limits import AGENT_RECURSION_LIMIT
from app.llm.native import AgentTurnLimit, continuation, first_prompt
from app.tracing import claude_trace_environment

if TYPE_CHECKING:
    # Host-only: agent images run this module without it.
    from app.llm.saved_conversations import ConversationRef

logger = logging.getLogger(__name__)
PLUGIN = Path(__file__).with_name('plugins') / 'langfuse-observability'
# Bounds exporting a transcript the plugin's hooks left behind.
TRACE_EXPORT_TIMEOUT = 120
# Tells the trace hook it runs the final export, after the CLI and its hooks.
FINAL_EXPORT_ENV = 'APP_CLAUDE_FINAL_EXPORT'
# Claude Code reports this in its User-Agent; the SDK would label every run 'sdk-py'.
ENTRYPOINT = 'temporal-sdlc'
MCP_TOOL_TIMEOUT_MS = 960_000
# Makes the CLI write its prompt cache with a 5-minute TTL, never a 1-hour one.
PROMPT_CACHING_5M_ENV = 'FORCE_PROMPT_CACHING_5M'


@dataclass
class ChatClaude(Runnable):
    model: str | None = None
    thinking_level: str = 'default'
    cli_path: str | None = None
    # Seconds without any message from Claude; a coding stage's many turns may take longer.
    # A tool call sends nothing while it runs, so this exceeds the longest shell command
    # (app.agent_workflows.runtime.SHELL_MAX_TIMEOUT).
    timeout: float = 1200
    # Coding stages keep native transcripts beside the project through a pause.
    cwd: Path | None = None
    config_dir: Path | None = None
    resume: str | None = None
    # The application conversation whose turns resume one native session, saved between
    # turns to the shared storage (app.llm.saved_conversations). Without the storage, or
    # with a cwd or config_dir of its own, every session starts anew.
    conversation: 'ConversationRef | None' = None
    # Write the prompt cache with a 5-minute TTL only (FORCE_PROMPT_CACHING_5M), whatever
    # the Claude login: for one-shot calls that gain nothing from a 1-hour cache.
    prompt_cache_5m: bool = False
    session_id: str | None = field(default=None, init=False)
    turns_used: int = field(default=0, init=False)
    _stack: AsyncExitStack | None = field(default=None, init=False, repr=False)
    _client: ClaudeSDKClient | None = field(default=None, init=False, repr=False)
    # The native session's ID, exported once more after the CLI exits.
    _native_session: str | None = field(default=None, init=False, repr=False)
    _messages: list[BaseMessage] = field(default_factory=list, init=False, repr=False)
    # The application messages this session started from, and whether its last turn
    # completed: what a saved conversation records.
    _history: list[BaseMessage] = field(default_factory=list, init=False, repr=False)
    _completed: bool = field(default=False, init=False, repr=False)
    # Notices for the model (see app.llm.graph.get_message_graph), shown after the next
    # tool result: Claude's loop takes no messages between its own requests.
    _notices: list[str] = field(default_factory=list, init=False, repr=False)

    @property
    def resumable_session(self) -> str | None:
        """The native session to continue, also while its turn has not ended: a stopped
        run resumes it (app.agent_workflows.runtime)."""
        return self._native_session or self.session_id

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
                      max_turns=AGENT_RECURSION_LIMIT, after_model_request=None,
                      **kwargs) -> AIMessage:
        messages = [HumanMessage(input)] if isinstance(input, str) else list(input)
        if not any(not isinstance(message, SystemMessage) for message in messages):
            raise ValueError('Claude needs at least one non-system message')
        if self._stack is None:
            async with self.async_session():
                return await self._turn(messages, tools, until, max_turns,
                                        after_model_request)
        return await self._turn(messages, tools, until, max_turns, after_model_request)

    def _take_notices(self) -> list[str]:
        notices, self._notices = self._notices, []
        return notices

    async def _turn(self, messages, tools, until, max_turns, after_model_request=None):
        async def stop_after_tools(data: HookInput, tool_use_id: str | None,
                                   context: HookContext) -> HookJSONOutput:
            if until is not None and until():
                return {'continue_': False, 'stopReason': 'Paused for the user'}
            return {}

        self._completed = False
        history = [message for message in messages if not isinstance(message, SystemMessage)]
        resumed = 0
        if self._client is None:
            stack = self._stack
            assert stack is not None  # ainvoke opens a session before any turn
            cwd, config_dir, resume = self.cwd, self.config_dir, self.resume
            store = None
            if self.conversation and cwd is None and config_dir is None:
                from app.llm import saved_conversations
                store = saved_conversations.get_store()
            if store is not None:
                # The CLI finds a transcript by its project directory, which is the cwd:
                # the same path for every conversation and worker. No tool writes there.
                cwd = Path(tempfile.gettempdir()) / 'claude-conversation'
                await run_sync(cwd.mkdir, parents=True, exist_ok=True)
                config_dir = Path(await stack.enter_async_context(
                    async_context(tempfile.TemporaryDirectory, prefix='claude-native-')))
                from app.llm.native_sessions import CLAUDE
                assert self.conversation is not None
                session, resumed = await CLAUDE.restore(store, self.conversation, history,
                                                        config_dir)
                if resumed:
                    resume = self._native_session = session
                self._history = history
                # Saved once the CLI and the final trace export are done.
                stack.push_async_exit(self._exit_saving(store, config_dir))
            if cwd is None:
                cwd = Path(await stack.enter_async_context(
                    async_context(tempfile.TemporaryDirectory, prefix='claude-native-')))
            env = claude_trace_environment()
            env.update(CLAUDE_CODE_ENTRYPOINT=ENTRYPOINT, APP_CLAUDE_HOOK_PYTHON=sys.executable,
                       PYTHONPATH=os.pathsep.join(filter(None, [str(Path(__file__).parents[2]),
                                                               os.getenv('PYTHONPATH')])))
            env['CC_LANGFUSE_STATE_DIR'] = str((config_dir or cwd) / '.langfuse-state')
            # The CLI's own limit on one tool call, in milliseconds: above the longest
            # shell command (app.agent_workflows.runtime.SHELL_MAX_TIMEOUT, 900 seconds).
            env['MCP_TOOL_TIMEOUT'] = str(MCP_TOOL_TIMEOUT_MS)
            if self.prompt_cache_5m:
                env[PROMPT_CACHING_5M_ENV] = '1'
            if config_dir is not None:
                await run_sync(config_dir.mkdir, parents=True, exist_ok=True, mode=0o700)
                env['CLAUDE_CONFIG_DIR'] = str(config_dir)
            native_tracing = env.get('APP_CLAUDE_TRACING_ENABLED') == 'true'
            if config_dir is None and native_tracing:
                # The plugin reads native transcripts. Keep them only for this host
                # session, under the same temporary-directory cleanup as its cwd.
                if self.cwd is not None:
                    cwd_config = Path(await stack.enter_async_context(
                        async_context(tempfile.TemporaryDirectory, prefix='claude-tracing-')))
                else:
                    cwd_config = cwd
                env['CLAUDE_CONFIG_DIR'] = str(cwd_config / '.claude-native')
                env['CC_LANGFUSE_STATE_DIR'] = str(cwd_config / '.langfuse-state')
            thinking: dict[str, Any] = ({} if self.thinking_level == 'default' else
                        {'thinking': {'type': 'disabled'}} if self.thinking_level == 'off' else
                        {'thinking': {'type': 'adaptive'}, 'effort': self.thinking_level})
            # A prompt can embed a whole API model, which Linux refuses as one process
            # argument. A coding stage's cwd is the user's project, so it stays out of there.
            system_dir = Path(await stack.enter_async_context(
                async_context(tempfile.TemporaryDirectory, prefix='claude-system-')))
            system_file = system_dir / 'system-prompt.md'
            await run_sync(system_file.write_text,
                           '\n\n'.join(message.text for message in messages
                                       if isinstance(message, SystemMessage)),
                           encoding='utf-8')
            options = ClaudeAgentOptions(
                model=self.model, cli_path=self.cli_path, cwd=str(cwd),
                system_prompt={'type': 'file', 'path': str(system_file)},
                **thinking, max_turns=max_turns, tools=[], skills=[], setting_sources=[],
                strict_mcp_config=True, verbatim_prompts=True,
                mcp_servers=({TOOL_SERVER: flow_server(tools, until=until,
                                                       notices=self._take_notices)}
                             if tools else {}),
                allowed_tools=[TOOL_PREFIX + item.name for item in tools],
                hooks=({'PostToolUse': [HookMatcher(matcher=TOOL_PREFIX + '.*',
                                                  hooks=[stop_after_tools])]}
                       if until else None),
                plugins=[{'type': 'local', 'path': str(PLUGIN)}] if native_tracing else [],
                extra_args=({'no-session-persistence': None}
                            if config_dir is None and not native_tracing else {}),
                env=env, resume=resume,
            )
            self._native_session = resume
            if native_tracing:
                # Runs once the CLI has exited and before its transcript is removed.
                stack.push_async_callback(
                    self._export_session, env, Path(env['CLAUDE_CONFIG_DIR']))
            self._client = await stack.enter_async_context(ClaudeSDKClient(options))
        # Follow-ups append only new user messages. Native history, thinking, tool
        # results and compaction stay inside Claude; no transcript reconstruction.
        if self._messages:
            if messages[:len(self._messages)] != self._messages:
                raise ValueError('A native Claude session only accepts appended messages')
            prompt = continuation([message for message in messages[len(self._messages):]
                                    if not isinstance(message, SystemMessage)])
        elif resumed:
            # A saved session holds the conversation up to its last reply.
            prompt = continuation(history[resumed:])
        else:
            prompt = first_prompt(messages)
        result = None
        blocks = []
        message_id = None
        model_name = self.model
        try:
            async with asyncio.timeout(self.timeout) as deadline:
                await self._client.query(prompt)
                async for event in self._client.receive_response():
                    deadline.reschedule(asyncio.get_running_loop().time() + self.timeout)
                    if isinstance(event, NativeSystemMessage) and event.subtype == 'init':
                        self._native_session = event.data.get('session_id') or self._native_session
                    if isinstance(event, AssistantMessage) and event.parent_tool_use_id is None:
                        if event.message_id != message_id:
                            blocks = []
                            if after_model_request is not None and (
                                    notice := after_model_request()):
                                self._notices.append(notice)
                        blocks.extend(event.content)
                        message_id = event.message_id
                        model_name = event.model
                    elif isinstance(event, ResultMessage):
                        result = event
                        self._native_session = event.session_id
        except TimeoutError as exc:
            await self._client.interrupt()
            raise TimeoutError(f'Claude sent nothing for {self.timeout:g} seconds') from exc
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
                raise AgentTurnLimit('Claude exceeded the tool-turn limit')
            raise RuntimeError('Claude response failed: ' + (
                '; '.join(result.errors or []) or result.result or result.subtype))
        reply = AIMessage(
            content=''.join(block.text for block in blocks if isinstance(block, TextBlock))
                    or result.result or '',
            response_metadata={'model_name': model_name, 'session_id': result.session_id,
                               'total_cost_usd': result.total_cost_usd},
        )
        self._messages = [*messages, reply]
        self._completed = True
        return reply

    def _exit_saving(self, store, config_dir: Path):
        async def save(exc_type, exc, traceback) -> bool:
            # A failed turn keeps the session saved before it, which its retry resumes.
            if exc_type is None and self._completed and self._native_session:
                await self._save(store, config_dir)
            return False
        return save

    async def _save(self, store, config_dir: Path) -> None:
        from app.llm.native_sessions import CLAUDE

        assert self.conversation is not None and self._native_session is not None
        await CLAUDE.save(store, self.conversation, self._history, self._native_session,
                          config_dir)

    async def _export_session(self, env: dict[str, str], config_dir: Path) -> None:
        """Final export of the session once the CLI has exited. The plugin's Stop hook
        exports completed requests as they finish, but never fires for an interrupted or
        failed one (such as error_max_turns), and a SessionEnd hook is cancelled by the
        closing CLI. This replaces SessionEnd; turns already exported are not repeated."""
        session = self._native_session
        if not session:
            return
        try:
            transcripts = await run_sync(
                lambda: sorted(config_dir.glob(f'projects/*/{session}.jsonl')))
            if not transcripts:
                return
            payload = json.dumps({'session_id': session, 'transcript_path': str(transcripts[0]),
                                  'hook_event_name': 'SessionEnd'})
            process = await asyncio.create_subprocess_exec(
                env['APP_CLAUDE_HOOK_PYTHON'], '-m', 'app.llm.claude_trace_hook',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **env, FINAL_EXPORT_ENV: '1'})
            try:
                _, errors = await asyncio.wait_for(process.communicate(payload.encode()),
                                                   TRACE_EXPORT_TIMEOUT)
            except TimeoutError:
                process.kill()
                await process.wait()
                logger.warning('Exporting the Claude session timed out')
                return
            if process.returncode:
                logger.warning('Exporting the Claude session failed (exit %s): %s',
                               process.returncode, errors.decode(errors='replace')[-4000:])
        except Exception:
            logger.warning('Could not export the Claude session', exc_info=True)

