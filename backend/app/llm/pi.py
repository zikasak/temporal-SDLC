"""Native Pi coding-agent sessions (`pi --mode rpc`): like ChatClaude, Pi owns its
history, its model/tool loop and its compaction, and calls the flow's tools over MCP.

A session runs one `pi` process. Beside it runs a loopback HTTP server that the process
alone may call, with a token of the session: the flow's tools as an MCP server
(app.llm.mcp), the decisions Pi's extension (pi_extension.ts) asks for after each turn,
and the relay its Langfuse plugin exports through (app.llm.pi_trace)."""
import asyncio
import base64
import hmac
import itertools
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import BaseModel, SecretStr

from app.asyncio_utils import async_context, run_sync
from app.llm.limits import AGENT_RECURSION_LIMIT
from app.llm.mcp import TOOL_PREFIX, TOOL_SERVER, flow_server
from app.llm.native import AgentTurnLimit, continuation, first_prompt
from app.tracing import native_trace_parent

if TYPE_CHECKING:
    # Host-only: agent images run this module without it.
    from app.llm.saved_conversations import ConversationRef

logger = logging.getLogger(__name__)
# Registers the endpoint's provider and the flow's MCP server, and asks after each turn
# whether the run goes on.
EXTENSION = Path(__file__).with_name('pi_extension.ts')
# What the extension reads from its environment: its configuration (no secret in it),
# the session's token and the endpoint's API key, which Pi reads by reference.
CONFIG_ENV = 'PI_FLOW_CONFIG'
TOKEN_ENV = 'PI_FLOW_TOKEN'
API_KEY_ENV = 'PI_FLOW_API_KEY'
# Sent in place of the key of an endpoint saved without one: Pi offers no model whose
# provider has no key.
NO_API_KEY = 'no-api-key'
# The provider the extension registers for an endpoint; no Pi login or models.json uses it.
ENDPOINT_PROVIDER = 'settings-endpoint'
# Google's endpoint is called through its native API, as its OpenAI-compatible one drops
# the thought signatures its models need back with their tool calls.
GOOGLE_HOST = 'generativelanguage.googleapis.com'
GOOGLE_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta'
# The context window Pi assumes for an endpoint's model, which reports none, when nothing
# says where to compact: Pi then compacts once the endpoint refuses a context too long.
ENDPOINT_CONTEXT_WINDOW = 1_000_000
# The free tokens below which Pi compacts (its compaction.reserveTokens default): an
# endpoint's model gets the window that starts compacting at its compaction_tokens.
COMPACTION_RESERVE = 16_384
# Pi's own override for its configuration directory.
AGENT_DIR_ENV = 'PI_CODING_AGENT_DIR'
# The Langfuse plugin's directory; without it, the plugin among npm's global packages.
PLUGIN_ENV = 'PI_LANGFUSE_PLUGIN'
PLUGIN_PACKAGE = '@langfuse/pi-observability-plugin'
# Pi runs only this application's extension (and its own MCP support): none of the
# user's local extensions, skills, templates, project context files or built-in tools.
PI_FLAGS = ('--mode', 'rpc', '--no-extensions', '--extension', str(EXTENSION),
            '--no-skills', '--no-prompt-templates', '--no-context-files', '--no-themes',
            '--no-approve')
# Seconds Pi waits for one tool call: above the longest shell command
# (app.agent_workflows.runtime.SHELL_MAX_TIMEOUT, 900 seconds).
MCP_TOOL_TIMEOUT = 960
# Seconds a closing Pi has to export its traces and save its session before it is killed.
SHUTDOWN_TIMEOUT = 60
# Seconds a command other than a prompt has to be answered.
COMMAND_TIMEOUT = 60
MAX_ERROR_CHARS = 2000
# Where the extension reports a turn, and where the plugin's exporter is sent.
TURN_PATH = '/turn'
MCP_PATH = '/mcp'
TRACE_PATH = '/langfuse'


class PiEndpoint(BaseModel):
    """An OpenAI-compatible endpoint (or Google's) that Pi calls in place of its own
    providers."""

    base_url: str
    api_key: SecretStr = SecretStr('')


@lru_cache(maxsize=1)
def langfuse_plugin() -> str | None:
    """The directory of the Langfuse plugin that traces Pi sessions, if installed."""
    configured = os.getenv(PLUGIN_ENV)
    if configured:
        path = Path(configured)
    else:
        try:
            root = subprocess.run(['npm', 'root', '--global'], capture_output=True,
                                  text=True, timeout=30, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            root = ''
        path = Path(root) / PLUGIN_PACKAGE if root else Path()
    if not path.joinpath('package.json').is_file():
        logger.warning('The Langfuse plugin for Pi (%s) is not installed; Pi sessions are '
                       'not traced', PLUGIN_PACKAGE)
        return None
    return str(path)


class _Server:
    """The loopback server of one Pi session (see the module's docstring)."""

    def __init__(self, token: str, tools: Sequence[BaseTool],
                 until: Callable[[], bool], decide: Callable[[dict], dict],
                 trace: dict[str, str] | None):
        self.token, self.tools, self.until = token, tools, until
        self.decide, self.trace = decide, trace

    def _bearer(self, headers) -> bool:
        return hmac.compare_digest(headers.get('authorization', ''), f'Bearer {self.token}')

    def _basic(self, headers) -> bool:
        """The plugin's exporter authenticates with the session's token as its secret."""
        value = headers.get('authorization', '')
        try:
            secret = base64.b64decode(value.removeprefix('Basic ')).decode().partition(':')[2]
        except ValueError:
            return False
        return value.startswith('Basic ') and hmac.compare_digest(secret, self.token)

    @asynccontextmanager
    async def serve(self):
        """Serve until the context exits; yields the server's URL."""
        import uvicorn
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route

        async def turn(request: Request):
            if not self._bearer(request.headers):
                return Response(status_code=401)
            return JSONResponse(self.decide(await request.json()))

        async def traces(request: Request):
            from app.llm import pi_trace

            if not self._basic(request.headers) or self.trace is None:
                return Response(status_code=401)
            try:
                body = await run_sync(
                    pi_trace.masked_export, await request.body(),
                    content_type=request.headers.get('content-type', ''),
                    encoding=request.headers.get('content-encoding', ''),
                    session_id=self.trace['session_id'], feature=self.trace['feature'])
            except Exception:
                logger.warning('Could not read the Pi session\'s observations', exc_info=True)
                # Sent again, it would fail again.
                return Response(status_code=400)
            try:
                status = await pi_trace.forward(body)
            except Exception:
                logger.warning('Could not export the Pi session\'s observations',
                               exc_info=True)
                return Response(status_code=502)
            # Langfuse's answer, which tells the exporter whether to send them again.
            return JSONResponse({}, status_code=200 if status < 300 else status)

        routes: list[Any] = [Route(TURN_PATH, turn, methods=['POST']),
                             Route(TRACE_PATH + '/api/public/otel/v1/traces', traces,
                                   methods=['POST'])]
        manager = None
        if self.tools:
            config = flow_server(self.tools, until=self.until)
            # Streamed: the response starts at once and is kept alive while a long tool
            # call (a shell command of up to SHELL_MAX_TIMEOUT) runs, where a plain JSON
            # response would send nothing until it ends.
            manager = StreamableHTTPSessionManager(config['instance'], stateless=True)

            session = self

            class Tools:
                """The MCP endpoint, as an ASGI app at exactly MCP_PATH (a mount would
                redirect its requests before checking them)."""

                async def __call__(self, scope, receive, send):
                    headers = {key.decode('latin-1').lower(): value.decode('latin-1')
                               for key, value in scope.get('headers', [])}
                    if not session._bearer(headers):
                        await Response(status_code=401)(scope, receive, send)
                        return
                    assert manager is not None
                    await manager.handle_request(scope, receive, send)

            routes.append(Route(MCP_PATH, Tools(), methods=['GET', 'POST', 'DELETE']))
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        server = _session_server(uvicorn.Config(Starlette(routes=routes), log_level='warning',
                                                lifespan='off', access_log=False))
        stopped = asyncio.Event()

        async def run() -> None:
            # The MCP sessions' task group lives in this task: errors of the session's
            # turns reach its callers as they are, not grouped by the task group.
            async with AsyncExitStack() as stack:
                if manager is not None:
                    await stack.enter_async_context(manager.run())
                serving = asyncio.create_task(server.serve(sockets=[listener]))
                waiting = asyncio.create_task(stopped.wait())
                try:
                    await asyncio.wait({serving, waiting},
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiting.cancel()
                    server.should_exit = True
                    await serving

        task = asyncio.create_task(run())
        try:
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError('The server of the Pi session stopped')
                await asyncio.sleep(0.01)
            yield f'http://127.0.0.1:{port}'
        finally:
            stopped.set()
            await task
            listener.close()


def _session_server(config):
    import uvicorn

    class SessionServer(uvicorn.Server):
        # The process's signals stay with the application (a coding agent stops on
        # SIGTERM), not with a server that lives as long as one session.
        @contextmanager
        def capture_signals(self):
            yield

    return SessionServer(config)


class _Process:
    """A `pi --mode rpc` process: its commands' responses and its stream of events."""

    def __init__(self, process: asyncio.subprocess.Process):
        self.process = process
        self.events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._responses: dict[str, asyncio.Future] = {}
        self._ids = itertools.count()
        self._stderr = b''
        self._reader = asyncio.create_task(self._read())
        self._errors = asyncio.create_task(self._read_errors())

    async def _read(self) -> None:
        assert self.process.stdout is not None
        pending = b''
        try:
            while chunk := await self.process.stdout.read(1 << 16):
                # Records are split on LF only: other Unicode line separators may occur
                # inside strings.
                *lines, pending = (pending + chunk).split(b'\n')
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        # Not a protocol record, such as an extension's stray output.
                        logger.debug('Pi wrote a line that is no record: %.200r', line)
                        continue
                    future = (self._responses.pop(record.get('id'), None)
                              if record.get('type') == 'response' else None)
                    if future is not None:
                        if not future.done():
                            future.set_result(record)
                    else:
                        self.events.put_nowait(record)
        finally:
            await self.process.wait()
            await asyncio.gather(self._errors, return_exceptions=True)
            for future in self._responses.values():
                if not future.done():
                    future.set_exception(RuntimeError(self.failure()))
            self._responses.clear()
            self.events.put_nowait(None)

    async def _read_errors(self) -> None:
        assert self.process.stderr is not None
        while chunk := await self.process.stderr.read(1 << 16):
            self._stderr = (self._stderr + chunk)[-MAX_ERROR_CHARS * 4:]

    def failure(self) -> str:
        errors = self._stderr.decode('utf-8', 'replace').strip()[-MAX_ERROR_CHARS:]
        return f'pi exited with code {self.process.returncode}: {errors}'

    async def send(self, command: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(command).encode() + b'\n')
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            await asyncio.wait({self._reader}, timeout=COMMAND_TIMEOUT)
            raise RuntimeError(self.failure()) from exc

    async def command(self, command: dict[str, Any],
                      timeout: float = COMMAND_TIMEOUT) -> dict[str, Any]:
        identifier = f'flow-{next(self._ids)}'
        future = asyncio.get_running_loop().create_future()
        self._responses[identifier] = future
        if self._reader.done():
            raise RuntimeError(self.failure())
        await self.send({**command, 'id': identifier})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._responses.pop(identifier, None)

    async def abort(self) -> None:
        """Stop the current run, if Pi still answers."""
        try:
            await self.command({'type': 'abort'}, COMMAND_TIMEOUT)
        except (TimeoutError, RuntimeError):
            logger.warning('Pi did not stop its run', exc_info=True)

    async def close(self) -> None:
        """Stop the current run and let Pi end its session (its plugin exports what is
        left), then kill it if it does not."""
        try:
            if self.process.returncode is None:
                # A run still going would otherwise be finished before Pi exits.
                await self.send({'type': 'abort'})
                assert self.process.stdin is not None
                self.process.stdin.close()
                await asyncio.wait_for(asyncio.shield(self._reader), SHUTDOWN_TIMEOUT)
        except (TimeoutError, RuntimeError):
            logger.warning('Pi did not exit within %d seconds; killing it', SHUTDOWN_TIMEOUT)
        finally:
            if self.process.returncode is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await asyncio.gather(self._reader, self._errors, return_exceptions=True)


@dataclass
class ChatPi(Runnable):
    """Credentials and the default model come from Pi's own configuration (`agent_dir`,
    else `~/.pi/agent`), or from `endpoint` when one is given."""

    # Pi model pattern such as `openai/gpt-4o` or `sonnet:high`; None uses Pi's default.
    # With an `endpoint`, the endpoint's own model ID.
    model: str | None = None
    endpoint: PiEndpoint | None = None
    thinking_level: str = 'default'
    command: str = 'pi'
    # Pi configuration directory, with its login; None leaves Pi to its environment and
    # default. An endpoint needs no login: Pi then gets an empty directory of its own.
    # Pi also connects the MCP servers of this directory's mcp.json, whose tools it is not
    # given (only the flow's are).
    agent_dir: str | None = None
    # Tokens in use at which Pi compacts a conversation with the endpoint's model, whose
    # context window it cannot know; 0 compacts only when the endpoint refuses a context
    # as too long. Pi's own providers compact by their models' real windows.
    compaction_tokens: int = 0
    # Seconds without any event from Pi; a coding stage's many turns may take longer.
    # A tool call sends nothing while it runs, so this exceeds MCP_TOOL_TIMEOUT.
    timeout: float = 1200
    # Coding stages keep the native session beside the project through a pause.
    cwd: Path | None = None
    config_dir: Path | None = None
    # The ID of a session in config_dir to continue.
    resume: str | None = None
    # The application conversation whose turns resume one native session, saved between
    # turns to the shared storage (app.llm.saved_conversations). Without the storage, or
    # with a config_dir of its own, every session starts anew.
    conversation: 'ConversationRef | None' = None
    session_id: str | None = field(default=None, init=False)
    turns_used: int = field(default=0, init=False)
    _stack: AsyncExitStack | None = field(default=None, init=False, repr=False)
    _process: _Process | None = field(default=None, init=False, repr=False)
    _messages: list[BaseMessage] = field(default_factory=list, init=False, repr=False)
    # The application messages this session started from, and whether its last turn
    # completed: what a saved conversation records.
    _history: list[BaseMessage] = field(default_factory=list, init=False, repr=False)
    _completed: bool = field(default=False, init=False, repr=False)
    # Notices for the model (see app.llm.graph.get_message_graph), shown before its next
    # request after tool results.
    _notices: list[str] = field(default_factory=list, init=False, repr=False)
    # The running turn's limits: Pi's extension asks after each of its model turns.
    _until: Callable[[], bool] | None = field(default=None, init=False, repr=False)
    _after_model_request: Callable[[], str | None] | None = field(
        default=None, init=False, repr=False)
    _max_turns: int = field(default=AGENT_RECURSION_LIMIT, init=False, repr=False)
    _run_turns: int = field(default=0, init=False, repr=False)
    _limited: bool = field(default=False, init=False, repr=False)
    # What failed while deciding after a turn, which stopped Pi and ends the run.
    _failure: BaseException | None = field(default=None, init=False, repr=False)

    @asynccontextmanager
    async def async_session(self):
        if self._stack is not None:
            raise RuntimeError('Pi session is already open')
        async with AsyncExitStack() as stack:
            self._stack = stack
            self._messages = []
            self.turns_used = 0
            try:
                yield self
            finally:
                self._process = None
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
            raise ValueError('Pi needs at least one non-system message')
        if self._stack is None:
            async with self.async_session():
                return await self._turn(messages, tools, until, max_turns,
                                        after_model_request)
        return await self._turn(messages, tools, until, max_turns, after_model_request)

    def _archive(self):
        from app.llm.native_sessions import PI, PI_ENDPOINT

        return PI_ENDPOINT if self.endpoint is not None else PI

    def _provider(self, endpoint: PiEndpoint) -> dict[str, Any]:
        """The extension's registration of `endpoint`, without its key."""
        google = urlparse(endpoint.base_url).hostname == GOOGLE_HOST
        return {
            'name': ENDPOINT_PROVIDER,
            'baseUrl': GOOGLE_BASE_URL if google else endpoint.base_url,
            'api': 'google-generative-ai' if google else 'openai-completions',
            'model': {
                'id': self.model, 'name': self.model,
                # Without a thinking level nothing about reasoning is sent: the model
                # uses its own default.
                'reasoning': self.thinking_level != 'default',
                'contextWindow': (self.compaction_tokens + COMPACTION_RESERVE
                                  if self.compaction_tokens else ENDPOINT_CONTEXT_WINDOW),
                # No output limit is sent: the endpoint applies its own.
                'maxTokens': 0,
                # Only standard fields, which every compatible server accepts.
                'compat': {'supportsStore': False, 'supportsDeveloperRole': False},
            },
        }

    def _decide(self, report: dict[str, Any]) -> dict[str, Any]:
        """Whether Pi goes on after a model turn, and what it reads first: the
        extension's question after each turn. A failure stops Pi and ends the run with
        it, rather than letting the turn go on unchecked."""
        try:
            return self._next(report)
        except Exception as exc:
            self._failure = exc
            return {'stop': True}

    def _next(self, report: dict[str, Any]) -> dict[str, Any]:
        # A failed or stopped request is retried or ends the run; it was no turn.
        if report.get('stopReason') in {'aborted', 'error'}:
            return {}
        self.turns_used += 1
        self._run_turns += 1
        if self._after_model_request is not None and (notice := self._after_model_request()):
            self._notices.append(notice)
        if not report.get('toolResults'):
            return {}
        if self._until is not None and self._until():
            return {'stop': True}
        if self._run_turns >= self._max_turns:
            self._limited = True
            return {'stop': True}
        notices, self._notices = self._notices, []
        return {'notices': notices} if notices else {}

    async def _turn(self, messages, tools, until, max_turns, after_model_request=None):
        self._completed = False
        history = [message for message in messages if not isinstance(message, SystemMessage)]
        resumed = 0
        self._until, self._after_model_request = until, after_model_request
        self._max_turns, self._run_turns, self._limited = max(1, max_turns), 0, False
        self._failure = None
        if self._process is None:
            resumed = await self._start(messages, tools, history)
        # Follow-ups append only new user messages. Native history, tool results and
        # compaction stay inside Pi; no transcript reconstruction.
        if self._messages:
            if messages[:len(self._messages)] != self._messages:
                raise ValueError('A native Pi session only accepts appended messages')
            prompt = continuation([message for message in messages[len(self._messages):]
                                   if not isinstance(message, SystemMessage)])
        elif resumed:
            # A saved session holds the conversation up to its last reply.
            prompt = continuation(history[resumed:])
        else:
            prompt = first_prompt(messages)
        reply = await self._run(prompt)
        self._messages = [*messages, reply]
        self._completed = True
        return reply

    async def _start(self, messages: list[BaseMessage], tools: Sequence[BaseTool],
                     history: list[BaseMessage]) -> int:
        """Start the session's Pi; returns how many of `history`'s messages a restored
        session holds."""
        stack = self._stack
        assert stack is not None  # ainvoke opens a session before any turn
        archive = self._archive()
        config_dir, resume, resumed = self.config_dir, self.resume, 0
        store = None
        if self.conversation and config_dir is None:
            from app.llm import saved_conversations
            store = saved_conversations.get_store()
        if config_dir is None:
            config_dir = Path(await stack.enter_async_context(
                async_context(tempfile.TemporaryDirectory, prefix='pi-native-')))
        await run_sync(config_dir.mkdir, parents=True, exist_ok=True, mode=0o700)
        if store is not None:
            assert self.conversation is not None
            session, resumed = await archive.restore(store, self.conversation, history,
                                                     config_dir)
            if resumed:
                resume = session
            self._history = history
            # Saved once Pi has exited and written all of its session.
            stack.push_async_exit(self._exit_saving(store, config_dir))
        cwd = self.cwd
        if cwd is None:
            # Pi names its working directory in the system prompt: one path for every
            # session keeps the prompt the provider cached. No tool works there.
            cwd = Path(tempfile.gettempdir()) / 'pi-conversation'
            await run_sync(cwd.mkdir, parents=True, exist_ok=True)
        command = [self.command, *PI_FLAGS, '--session-dir', str(config_dir / 'sessions')]
        if resume:
            transcripts = await run_sync(archive.transcripts, config_dir, resume)
            if not transcripts:
                raise ValueError(f'The Pi session {resume} is not in {config_dir}')
            command += ['--session', str(transcripts[-1])]
        # Naming the flow's tools keeps Pi's own read/bash/edit/write tools switched off,
        # and any other MCP server's.
        command += (['--extension', 'builtin:mcp', '--tools', TOOL_PREFIX + '*'] if tools
                    else ['--no-tools'])
        config: dict[str, Any] = {'server': TOOL_SERVER, 'tools': bool(tools),
                                  'toolTimeout': MCP_TOOL_TIMEOUT}
        token = secrets.token_urlsafe(32)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('LANGFUSE_')}
        environment[TOKEN_ENV] = token
        # Pi asks for no newer version of itself: the images pin it.
        environment['PI_SKIP_VERSION_CHECK'] = '1'
        if self.endpoint is not None:
            # The endpoint needs no Pi login: a directory of its own keeps out the
            # settings and MCP servers of whatever Pi directory the host has.
            environment[AGENT_DIR_ENV] = await stack.enter_async_context(
                async_context(tempfile.TemporaryDirectory, prefix='pi-agent-'))
        elif self.agent_dir:
            environment[AGENT_DIR_ENV] = self.agent_dir
        if self.endpoint is not None:
            if not self.model:
                raise ValueError('pi needs a model to call an endpoint')
            command += ['--provider', ENDPOINT_PROVIDER, '--model', self.model]
            config['provider'] = self._provider(self.endpoint)
            environment[API_KEY_ENV] = self.endpoint.api_key.get_secret_value() or NO_API_KEY
        elif self.model:
            command += ['--model', self.model]
        if self.thinking_level != 'default':
            command += ['--thinking', self.thinking_level]
        trace = native_trace_parent()
        plugin = await run_sync(langfuse_plugin) if trace is not None else None
        server = _Server(token, tools, lambda: self._until is not None and self._until(),
                         self._decide, trace if plugin else None)
        url = await stack.enter_async_context(server.serve())
        config['url'] = url
        if trace is not None and plugin is not None:
            command += ['--extension', plugin]
            environment.update(self._trace_environment(trace, url, token))
        else:
            environment['LANGFUSE_TRACING_ENABLED'] = 'false'
        # The extension sends this prompt as it is, in place of Pi's own (which describes
        # its coding tools and working directory). A prompt can embed a whole API model,
        # too long for the environment, so it goes in a file. A coding stage's cwd is the
        # user's project, so it stays out of there.
        system_dir = Path(await stack.enter_async_context(
            async_context(tempfile.TemporaryDirectory, prefix='pi-system-')))
        system_file = system_dir / 'system-prompt.md'
        await run_sync(system_file.write_text,
                       '\n\n'.join(message.text for message in messages
                                    if isinstance(message, SystemMessage)),
                       encoding='utf-8')
        config['systemPrompt'] = str(system_file)
        environment[CONFIG_ENV] = json.dumps(config)
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=cwd, env=environment, start_new_session=True)
        pi = _Process(process)
        stack.push_async_callback(pi.close)
        self._process = pi
        state = await pi.command({'type': 'get_state'}, self.timeout)
        if not state.get('success'):
            raise RuntimeError(f'pi did not start: {state.get("error")}')
        self.session_id = state['data']['sessionId']
        return resumed

    @staticmethod
    def _trace_environment(trace: dict[str, str], url: str, token: str) -> dict[str, str]:
        """The plugin's configuration: nested under the current observation (as the
        plugin nests a Pi process another one started), in the application's session,
        exporting through the session's relay with the session's token as its secret."""
        environment = {'LANGFUSE_PUBLIC_KEY': os.environ['LANGFUSE_PUBLIC_KEY'],
                       'LANGFUSE_SECRET_KEY': token, 'LANGFUSE_BASE_URL': url + TRACE_PATH,
                       # Images would otherwise upload outside the relay's masking.
                       'LANGFUSE_MEDIA_UPLOAD_ENABLED': 'false'}
        for key in ('LANGFUSE_TRACING_ENVIRONMENT', 'LANGFUSE_RELEASE'):
            if os.getenv(key):
                environment[key] = os.environ[key]
        if trace['trace_id'] and trace['span_id']:
            environment.update(LANGFUSE_PI_PARENT_TRACE_ID=trace['trace_id'],
                               LANGFUSE_PI_PARENT_SPAN_ID=trace['span_id'],
                               LANGFUSE_PI_PARENT_SESSION_ID=trace['session_id'])
        return environment

    async def _run(self, prompt: str) -> AIMessage:
        """One prompt, until Pi settles; its final reply."""
        pi = self._process
        assert pi is not None
        response = await pi.command({'type': 'prompt', 'message': prompt}, self.timeout)
        if not response.get('success'):
            raise RuntimeError(f'pi refused the prompt: {response.get("error")}')
        last = answer = None
        cost = 0.0
        while True:
            try:
                event = await asyncio.wait_for(pi.events.get(), self.timeout)
            except TimeoutError as exc:
                await pi.abort()
                raise TimeoutError(f'Pi sent nothing for {self.timeout:g} seconds') from exc
            if event is None:
                raise RuntimeError(pi.failure())
            if event.get('type') == 'agent_settled':
                break
            if event.get('type') == 'message_end' and event['message'].get('role') == 'assistant':
                last = event['message']
                cost += ((last.get('usage') or {}).get('cost') or {}).get('total', 0)
                if last.get('stopReason') not in {'aborted', 'error'}:
                    answer = last
        if self._failure is not None:
            raise self._failure
        paused = self._until is not None and self._until()
        if self._limited and not paused:
            raise AgentTurnLimit('Pi exceeded the tool-turn limit')
        if last is None:
            raise RuntimeError('pi produced no assistant response')
        # Pi retries a transient failure itself; one it gave up on ends the run.
        if last.get('stopReason') == 'error' and not paused:
            raise RuntimeError(f'pi response failed: {last.get("errorMessage") or "error"}')
        if last.get('stopReason') == 'aborted' and not (paused or self._limited):
            raise RuntimeError('pi response was aborted')
        reply = answer or {}
        return AIMessage(
            content=''.join(block['text'] for block in reply.get('content', [])
                            if block.get('type') == 'text'),
            response_metadata={'model_name': reply.get('model'),
                               'model_provider': reply.get('provider'),
                               'finish_reason': reply.get('stopReason'),
                               'session_id': self.session_id, 'total_cost_usd': cost},
        )

    def _exit_saving(self, store, config_dir: Path):
        async def save(exc_type, exc, traceback) -> bool:
            # A failed turn keeps the session saved before it, which its retry resumes.
            if exc_type is None and self._completed and self.session_id:
                assert self.conversation is not None
                await self._archive().save(store, self.conversation, self._history,
                                           self.session_id, config_dir)
            return False
        return save
