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


class ClaudeTurnLimit(RuntimeError):
    """The native agent used the allowed number of model turns."""


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
                      max_turns=AGENT_RECURSION_LIMIT, **kwargs) -> AIMessage:
        messages = [HumanMessage(input)] if isinstance(input, str) else list(input)
        if not any(not isinstance(message, SystemMessage) for message in messages):
            raise ValueError('Claude needs at least one non-system message')
        if self._stack is None:
            async with self.async_session():
                return await self._turn(messages, tools, until, max_turns)
        return await self._turn(messages, tools, until, max_turns)

    async def _turn(self, messages, tools, until, max_turns):
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
                resumed = await self._restore(store, history, config_dir)
                if resumed:
                    resume = self._native_session
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
                mcp_servers=({TOOL_SERVER: flow_server(tools, until=until)} if tools else {}),
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
            prompt = _continuation([message for message in messages[len(self._messages):]
                                    if not isinstance(message, SystemMessage)])
        elif resumed:
            # A saved session holds the conversation up to its last reply.
            prompt = _continuation(history[resumed:])
        else:
            prompt = _prompt(messages)
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
        self._completed = True
        return reply

    async def _restore(self, store, history: list[BaseMessage], config_dir: Path) -> int:
        """Restore the conversation's saved session into `config_dir`; returns how many
        of `history`'s messages it holds, 0 when this turn starts a new session."""
        from app.llm import claude_sessions

        assert self.conversation is not None
        try:
            data = await run_sync(store.load,
                                  claude_sessions.object_name(self.conversation.source))
            if data is None:
                return 0
            meta = await run_sync(claude_sessions.unpack, data, config_dir)
        except Exception:
            logger.warning('Could not restore the Claude session of %s; starting a new one',
                           self.conversation.source, exc_info=True)
            return 0
        resumed = await run_sync(claude_sessions.resumable, meta, history, config_dir,
                                 self.conversation.context)
        if resumed:
            self._native_session = meta['session_id']
            logger.info('Resuming Claude session %s of %s after %d messages',
                        meta['session_id'], self.conversation.source, resumed)
        else:
            logger.info('The saved Claude session of %s does not continue this '
                        'conversation; starting a new one', self.conversation.source)
            # Not saved again with the new session.
            await run_sync(claude_sessions.clear, config_dir)
        return resumed

    def _exit_saving(self, store, config_dir: Path):
        async def save(exc_type, exc, traceback) -> bool:
            # A failed turn keeps the session saved before it, which its retry resumes.
            if exc_type is None and self._completed and self._native_session:
                await self._save(store, config_dir)
            return False
        return save

    async def _save(self, store, config_dir: Path) -> None:
        from app.llm import claude_sessions
        from app.llm.saved_conversations import saved_meta

        assert self.conversation is not None
        history = self._history
        # The conversation goes on from this session's first messages and its final
        # reply; the corrections a turn sent in between are not part of it.
        meta = {'session_id': self._native_session,
                **saved_meta(history, self.conversation.context)}
        try:
            data = await run_sync(claude_sessions.pack, config_dir, meta)
            await run_sync(store.save, claude_sessions.object_name(self.conversation.name),
                           data)
        except Exception:
            logger.warning('Could not save the Claude session of %s', self.conversation.name,
                           exc_info=True)
            return
        logger.info('Saved Claude session %s of %s (%d bytes)', self._native_session,
                    self.conversation.name, len(data))

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


def _prompt(messages: list[BaseMessage]) -> str:
    history = [message for message in messages if not isinstance(message, SystemMessage)]
    if len(history) == 1 and isinstance(history[0], HumanMessage):
        return history[0].text
    # Application conversations are context for a new native session, never forged
    # Claude transcript rows. Subsequent requests use the native session itself.
    return 'Conversation context:\n' + json.dumps([
        {'role': message.type, 'content': message.content} for message in history
    ], ensure_ascii=False) + '\n\nContinue the conversation and complete the latest user request.'


def _continuation(added: list[BaseMessage]) -> str:
    """The prompt that appends `added` to a native session: the user's text, or the
    whole exchange when it also holds replies the session has not seen."""
    if all(isinstance(message, HumanMessage) for message in added):
        return '\n\n'.join(message.text for message in added)
    return 'Conversation since your last reply:\n' + json.dumps([
        {'role': message.type, 'content': message.content} for message in added
    ], ensure_ascii=False) + '\n\nContinue the conversation and complete the latest user request.'


class ClaudeGraph:
    """The flow's invocation interface, with the SDK owning all model/tool turns."""

    def __init__(self, model: ChatClaude, tools, until):
        self.model, self.tools, self.until = model, tools, until

    async def ainvoke(self, state, config=None):
        try:
            reply = await self.model.ainvoke(
                state['messages'], tools=self.tools, until=self.until,
                max_turns=max(1, (config or {}).get('recursion_limit', AGENT_RECURSION_LIMIT)),
            )
        except ClaudeTurnLimit as exc:
            from langgraph.errors import GraphRecursionError
            raise GraphRecursionError(str(exc)) from exc
        return {'messages': [*state['messages'], reply]}
