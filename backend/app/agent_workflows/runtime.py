"""Container-only tools and bounded model loop. No host config, Temporal or Docker."""
import asyncio
import fnmatch
import itertools
import json
import logging
import os
import re
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, messages_from_dict,
    messages_to_dict,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool, tool
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.asyncio_utils import async_context, run_sync
from app.llm.factory import create_adapter
from app.llm.limits import LIMIT_FILENAME, PROVIDER_LIMIT_EXIT, is_limit_error, limit_report
from app.settings.models import ModelSettings, SETTINGS_FILENAME
from app.llm.questions import (
    ASK_USER, DESCRIPTION, AskUserInput, answers_result, asks_directly, normalize_questions,
)
from app.llm.limits import AGENT_RECURSION_LIMIT
from app.tracing import (
    container_trace_context, langchain_config, shutdown_tracing, trace_operation,
)


@dataclass(frozen=True)
class AgentProfile:
    root: Path
    prompt: str
    initial_messages: Sequence[str]
    max_turns_env: str
    validate_completion: Callable[[Path], None]
    completion_message: str
    exhaustion_message: str
    default_max_turns: int = AGENT_RECURSION_LIMIT
    # Read-only contract files supplied instead of embedding large contracts in full.
    contracts: Path | None = None
    # Directory names holding the language's build output and installed dependencies.
    build_dirs: Sequence[str] = ()
    # For report-only stages, writing this file ends the run after validation.
    completion_file: str | None = None
    # Replaces CHECKPOINT_PROMPT for stages whose progress is not code on disk.
    checkpoint_prompt: str | None = None
    # Offers the note tool, whose notes are kept verbatim across history compaction.
    working_notes: bool = False
    # For report-only stages: when the turns run out without the completion file, one
    # last request without tools writes it from the notes, the checkpoint and the latest
    # history, as this prompt asks. The reply becomes the file's content.
    wrap_up_prompt: str | None = None


def _max_turns(profile: AgentProfile) -> int:
    """The stage's tool-turn budget from its env var; unset, blank or invalid values
    fall back to the profile default."""
    value = os.getenv(profile.max_turns_env, '').strip()
    try:
        return int(value) if value else profile.default_max_turns
    except ValueError:
        logging.getLogger(__name__).warning(
            'Ignoring invalid %s=%r; using %d', profile.max_turns_env, value,
            profile.default_max_turns)
        return profile.default_max_turns


READ_LINES = 500
READ_CHARS = 30000
# A shell command's default and longest time. The longest must stay below the time a
# coding agent's model may go silent (app.llm.claude.ChatClaude.timeout), which a command
# running in a tool call counts towards.
SHELL_TIMEOUT = 120
SHELL_MAX_TIMEOUT = 900
SHELL_CHARS = 8000
SHELL_TAIL_CHARS = 3000
DIAGNOSTIC_LINES = 40
SEARCH_MATCHES = 50
LISTED_FILES = 200
LINE_CHARS = 300
# Verbose command output is kept here for explicit retrieval; stages never publish it.
OUTPUT_DIR = '.agent-output'
# Never listed or searched, besides the build directories of the project's language.
SKIPPED_DIRS = {OUTPUT_DIR, '.git'}
# Share of the compaction threshold (ModelSettings.compaction_tokens) that compaction
# keeps verbatim as the most recent batches.
KEEP_RECENT = 1 / 3
# Code and JSON run near three characters a token; used when the provider reports no usage.
TOKEN_CHARS = 3
CLIP_CHARS = 4000
CHECKPOINT_PROMPT = """You keep the working notes of a coding agent. The oldest part of its
tool history is about to be removed from its context. Write the checkpoint it will continue
from, merging any previous checkpoint with the history below. Use these sections:
Decisions: design choices, conventions and facts learned that the files do not make obvious.
Done: work that is complete and verified.
Remaining: work still to do, in order.
Unresolved errors: exact messages and file locations of failures not yet fixed.
Current file contents stay on disk and will be re-read, so do not reproduce code.
Be specific and concise. Record only what the history shows; never guess.
"""
# Without this, the summary takes its own clipping for truncated reads and asks for rereads.
CLIP_NOTICE = """
Long calls and results in the history are shortened for this summary only, where a marker
says characters were omitted here only. The agent saw them in full: never record a read or
an output as truncated or incomplete because of these markers.
"""
NOTE = 'note'
NOTE_CHARS = 1000
CHECKPOINT_NOTICE = ('[Checkpoint] Older tool calls and their results were removed to save '
                     'context. Files on disk are the source of truth: re-read the regions '
                     'you need before changing them.')
# A run that asked the user something exits with this code once it has saved where it
# stopped, beside its project: the host keeps the workspace and starts a new container
# from it, with the answers, once they arrive.
PAUSE_EXIT = 75
STATE_FILENAME = '.agent-state.json'
ANSWERS_FILENAME = '.agent-answers.json'
ASK_GUIDANCE = """
You can ask the user with the ask_user tool when the task leaves open a decision that
changes the result and that neither the inputs nor the files settle. The run pauses until
the user answers, which may take long, so ask only what you cannot reasonably decide
yourself, and put every question you have into one call. Never ask a question in your
text: nobody reads it, and a reply without a tool call ends the run.
"""
# How often a final reply that asks something in its text is sent back.
DIRECT_QUESTION_NUDGES = 2
DIRECT_QUESTION_NOTICE = ('[System] Your last message asked a question in its text, but '
                          'nobody reads it: the run ends with a reply that calls no tool. To '
                          'ask the user, call ask_user. Otherwise decide yourself, finish the '
                          'work, and end with a summary that asks nothing.')
FINISHING_TURNS = 10
# What a native session that the provider's limits stopped is told when it goes on.
LIMIT_CONTINUE = ('[System] The model provider was unavailable for a while and the run was '
                  'stopped. Continue the task where you left off; check the files you were '
                  'working on before you change them.')
TURN_BUDGET = """
You have {turns} model turns for this task; each reply of yours is one, however many tool
calls it makes. Plan the work to fit, and batch independent tool calls into one turn.
"""
DIAGNOSTIC = re.compile(r'\[ERROR\]|\berror\b|BUILD (SUCCESS|FAILURE)|Exception|FAILED|Tests run:')


def workspace_tools(root: Path, contracts: Path | None = None,
                    build_dirs: Sequence[str] = ()) -> list[BaseTool]:
    root = root.resolve()
    skipped = SKIPPED_DIRS | set(build_dirs)
    commands = itertools.count(1)

    def workspace_path(path: str, base: Path = root, area: str = 'project workspace') -> Path:
        result = (base / path).resolve()
        if not result.is_relative_to(base):
            raise ValueError(f'Path must be inside the {area}')
        return result

    def workspace_files(start: Path, pattern: str | None = None,
                        base: Path = root) -> Iterator[Path]:
        """Regular files in stable order, never links, build output or saved logs."""
        if start.is_file():
            yield start
            return
        for directory, names, files in os.walk(start):
            names[:] = sorted(name for name in names if name not in skipped)
            for name in sorted(files):
                item = Path(directory, name)
                relative = item.relative_to(base).as_posix()
                if item.is_symlink() or (pattern and not (fnmatch.fnmatch(relative, pattern)
                                                          or fnmatch.fnmatch(name, pattern))):
                    continue
                yield item

    def read_lines(target: Path, offset: int, limit: int) -> str:
        lines = target.read_text().splitlines(keepends=True)
        first = max(offset, 1)
        selected, size = [], 0
        for line in lines[first - 1:first - 1 + max(limit, 1)]:
            if selected and size + len(line) > READ_CHARS:
                break
            selected.append(line)
            size += len(line)
        text = ''.join(selected)
        if size > READ_CHARS:
            text = (text[:READ_CHARS] + f'\n[Line {first} truncated at {READ_CHARS} '
                    'characters; use shell to inspect the rest of it.]')
        last = first - 1 + len(selected)
        if first > 1 or last < len(lines):
            more = f' Continue with offset={last + 1}.' if last < len(lines) else ''
            text += f'\n[Showing lines {first}-{last} of {len(lines)}.{more}]'
        return text

    def search_lines(regex: str, start: Path, pattern: str | None = None,
                     base: Path = root) -> str:
        expression = re.compile(regex)
        matches = []
        for item in workspace_files(start, pattern, base):
            try:
                lines = item.read_text().splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(lines, 1):
                if expression.search(line):
                    if len(matches) == SEARCH_MATCHES:
                        matches.append(f'[More than {SEARCH_MATCHES} matches; narrow the search.]')
                        return '\n'.join(matches)
                    matches.append(f'{item.relative_to(base).as_posix()}:{number}: '
                                   f'{line.strip()[:LINE_CHARS]}')
        return '\n'.join(matches) or 'No matches'

    @tool
    def read_file(path: str, offset: int = 1, limit: int = READ_LINES) -> str:
        """Read a UTF-8 file relative to the project root.

        Returns up to `limit` lines starting at 1-based line `offset`. A final note
        says how to continue when more of the file remains.
        """
        return read_lines(workspace_path(path), offset, limit)

    @tool
    def write_file(path: str, content: str) -> str:
        """Create a new file, or replace a whole file, relative to the project root.

        Prefer edit_file for changes to an existing file.
        """
        target = workspace_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return 'Written'

    @tool
    def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        """Replace exact text in an existing file relative to the project root.

        `old_text` must match exactly once, including whitespace, unless `replace_all`
        is true. Include enough surrounding lines to make the match unique.
        """
        target = workspace_path(path)
        content = target.read_text()
        count = content.count(old_text) if old_text else 0
        if count == 0:
            raise ValueError('old_text was not found; read the file and match it exactly')
        if count > 1 and not replace_all:
            raise ValueError(f'old_text matches {count} places; add surrounding lines '
                             'or set replace_all')
        target.write_text(content.replace(old_text, new_text))
        return f'Edited ({count} replacement{"s" if count > 1 else ""})'

    @tool
    def list_files(path: str = '.', pattern: str | None = None) -> str:
        """List files under a directory relative to the project root.

        `pattern` is an optional glob such as `*.java` or `src/main/*Controller.java`.
        Build output and version-control directories are skipped.
        """
        found = [item.relative_to(root).as_posix()
                 for item in itertools.islice(workspace_files(workspace_path(path), pattern),
                                              LISTED_FILES + 1)]
        if not found:
            return 'No files found'
        if len(found) > LISTED_FILES:
            found[LISTED_FILES:] = [f'[More than {LISTED_FILES} files; narrow path or pattern.]']
        return '\n'.join(found)

    @tool
    def search_files(regex: str, path: str = '.', pattern: str | None = None) -> str:
        """Search file contents for a regular expression, returning `file:line: text`.

        `path` is a file or directory relative to the project root and `pattern` an
        optional file glob such as `*.java`. Use this to locate code before reading it.
        """
        return search_lines(regex, workspace_path(path), pattern)

    @tool
    async def shell(command: str, timeout_seconds: int = SHELL_TIMEOUT) -> str:
        """Run a shell command in the project. It is stopped after `timeout_seconds`
        (default 120, at most 900): give a test suite or a checks script the time it needs
        and run it whole in one call, rather than splitting it or polling a background
        run. Processes it starts in the background end with it: start a server and use it
        in one command.

        Long output is saved to a log file and summarized as its error lines and tail;
        the summary names the log so read_file or search_files can retrieve the rest.
        """
        limit = min(max(int(timeout_seconds), 1), SHELL_MAX_TIMEOUT)
        logs = root / OUTPUT_DIR
        await run_sync(logs.mkdir, exist_ok=True)
        log = logs / f'shell-{next(commands)}.log'
        async with async_context(log.open, 'wb') as output:
            process = await asyncio.create_subprocess_shell(
                command, cwd=root, stdout=output, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True)
            try:
                await asyncio.wait_for(process.wait(), timeout=limit)
                status = f'Exit code: {process.returncode}'
            except BaseException as exc:
                _end_session(process.pid)
                await process.wait()
                if not isinstance(exc, TimeoutError):
                    raise
                status = f'Command timed out after {limit} seconds'
            # What the command left running in the background goes too: such processes
            # kept every thread they had until the container hit its process limit.
            _end_session(process.pid)
        return await run_sync(shell_output, log, status)

    def shell_output(log: Path, status: str) -> str:
        if log.stat().st_size <= SHELL_CHARS:
            text = log.read_text('utf-8', 'replace')
            log.unlink()
            return f'{status}\n{text}'
        diagnostics, total = [], 0
        with log.open(encoding='utf-8', errors='replace') as output:
            for total, line in enumerate(output, 1):
                if len(diagnostics) <= DIAGNOSTIC_LINES and DIAGNOSTIC.search(line):
                    diagnostics.append(f'{total}: {line.strip()[:LINE_CHARS]}')
        if len(diagnostics) > DIAGNOSTIC_LINES:
            diagnostics[DIAGNOSTIC_LINES:] = ['[More diagnostic lines are in the log.]']
        with log.open('rb') as output:
            output.seek(-SHELL_TAIL_CHARS, 2)
            tail = output.read().decode('utf-8', 'replace')
        relative = log.relative_to(root).as_posix()
        return '\n'.join([
            status,
            f'[Output was {total} lines; the full log is {relative}. Read more of it with '
            f'read_file offset/limit or search_files.]',
            *(['Diagnostic lines (log line number: text):', *diagnostics] if diagnostics else []),
            'End of output:',
            tail,
        ])

    tools = [read_file, write_file, edit_file, list_files, search_files, shell]
    for item in tools:
        assert isinstance(item, StructuredTool)  # made by @tool
        if item.coroutine is None:
            assert item.func is not None  # a tool without a coroutine has a function
            item.coroutine = partial(run_sync, item.func)
    if contracts is None:
        return tools
    contracts = contracts.resolve()

    @tool
    def read_contract(name: str, offset: int = 1, limit: int = READ_LINES) -> str:
        """Read a contract file named in the contract index, such as `openapi.yaml`.

        Contract files are the read-only authority and live outside the project. Takes
        the same 1-based line `offset` and `limit` as read_file.
        """
        return read_lines(workspace_path(name, contracts, 'contracts directory'), offset, limit)

    @tool
    def search_contracts(regex: str, name: str = '.') -> str:
        """Search contract files for a regular expression, returning `file:line: text`.

        `name` limits the search to one contract file. Use this to find a requirement
        ID, endpoint path or schema name, then read around it with read_contract.
        """
        return search_lines(regex, workspace_path(name, contracts, 'contracts directory'),
                            base=contracts)

    for item in (read_contract, search_contracts):
        assert isinstance(item, StructuredTool) and item.func is not None  # @tool of a function
        item.coroutine = partial(run_sync, item.func)
    return [*tools, read_contract, search_contracts]


def _end_session(pid: int) -> None:
    """Kill the process group a shell command led, with what its descendants left in it."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _tokens(message: BaseMessage) -> int:
    calls = getattr(message, 'tool_calls', None) or []
    return (len(str(message.content)) + len(json.dumps(calls, default=str))) // TOKEN_CHARS + 1


def _recent_start(history: Sequence[BaseMessage], keep_tokens: float) -> int:
    """Index of the first batch to keep. Only whole assistant/tool-result batches are
    dropped, so every remaining result still follows its call; the latest always stays."""
    start, kept = len(history), 0
    for index in range(len(history) - 1, -1, -1):
        kept += _tokens(history[index])
        if isinstance(history[index], AIMessage):
            if start < len(history) and kept > keep_tokens:
                break
            start = index
    return start


def _clip(text: str) -> str:
    if len(text) <= CLIP_CHARS:
        return text
    half = CLIP_CHARS // 2
    return (f'{text[:half]}\n[... {len(text) - CLIP_CHARS} characters omitted here only; '
            f'the agent saw them ...]\n{text[-half:]}')


def _named(config: RunnableConfig, run_name: str) -> RunnableConfig:
    """`config` for a run of its own name."""
    return {**config, 'run_name': run_name}


def _transcript(messages: Sequence[BaseMessage]) -> str:
    lines = []
    for message in messages:
        if isinstance(message, ToolMessage):
            lines.append(f'Result: {_clip(str(message.content))}')
            continue
        if message.text:
            lines.append(f'Agent: {_clip(message.text)}')
        # Only the agent's own messages call tools; a notice sent to it calls none.
        if isinstance(message, AIMessage):
            lines += [f'Call {call["name"]}: {_clip(json.dumps(call["args"], ensure_ascii=False))}'
                      for call in message.tool_calls]
    return '\n'.join(lines)


def ask_user_tool() -> BaseTool:
    """Declared to the model only: the loop pauses the run instead of calling it."""
    def ask_user(questions: list) -> str:
        raise RuntimeError('ask_user pauses the run')

    return StructuredTool.from_function(ask_user, name=ASK_USER, description=DESCRIPTION,
                                        args_schema=AskUserInput)


def note_tool(notes: list[str]) -> BaseTool:
    def note(text: str) -> str:
        """Record a conclusion you must not lose, such as a suspected or confirmed problem
        or a region you finished. Older tool results are removed as the context fills;
        your notes are kept word for word. Call it alongside your next tool calls."""
        notes.append(text.strip()[:NOTE_CHARS])
        return 'Noted.'

    return StructuredTool.from_function(note, name=NOTE)


# How often, and how long at most each time, a request the provider refused for its limits
# is repeated inside the container before the run stops and the host waits instead.
LIMIT_RETRIES = 2
LIMIT_RETRY_SECONDS = 60
# What a run of the agent ended with.
DONE, PAUSED, LIMITED, STOPPED = 'done', 'paused', 'limited', 'stopped'


class ProviderLimit(Exception):
    """The provider refused requests for its limits, also after the retries: the run saves
    where it stopped and ends for the host to continue it once the provider is available."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.report = limit_report(cause)


async def _request(call: Callable[[], Any]) -> Any:
    """Make a model request; one refused for the provider's limits is repeated a few
    times after the wait it asks for, then raised as ProviderLimit."""
    for attempt in range(LIMIT_RETRIES + 1):
        try:
            return await call()
        except Exception as exc:
            if not is_limit_error(exc):
                raise
            if attempt == LIMIT_RETRIES:
                raise ProviderLimit(exc) from exc
            wait = limit_report(exc)['retry_after']
            wait = min(LIMIT_RETRY_SECONDS, 15 * 2 ** attempt if wait is None else wait)
            if wait >= LIMIT_RETRY_SECONDS and attempt:
                # A long wait is the host's: it frees the worker meanwhile.
                raise ProviderLimit(exc) from exc
            print(f'Provider limit reached; retrying in {wait:.0f} seconds', flush=True)
            await asyncio.sleep(wait)


def _stop_for_limit(directory: Path, state: dict[str, Any], limit: ProviderLimit) -> None:
    """Save where the run stopped, as a pause does but with no question pending, and what
    the provider said, for the host."""
    _pause(directory, {**state, 'pending': None})
    (directory / LIMIT_FILENAME).write_text(json.dumps(limit.report, ensure_ascii=False))
    print(f'Stopped until the provider is available: {limit.report["message"]}', flush=True)


def _pause(directory: Path, state: dict[str, Any]) -> None:
    """Save where the run stopped, for the run that continues it once the user answers."""
    temporary = directory / f'{STATE_FILENAME}.tmp'
    temporary.write_text(json.dumps(state, ensure_ascii=False))
    os.replace(temporary, directory / STATE_FILENAME)


def _resume(directory: Path) -> dict[str, Any] | None:
    """The saved state of a paused run, its ask_user call answered, or of a run its
    provider's limits stopped; None for a new run."""
    state_file, answers_file = directory / STATE_FILENAME, directory / ANSWERS_FILENAME
    if not state_file.is_file():
        return None
    state = json.loads(state_file.read_text())
    pending = state['pending']
    history = messages_from_dict(state['history'])
    if pending is not None:
        answers = json.loads(answers_file.read_text())['answers']
        answers_file.unlink()
        history.insert(pending['position'], ToolMessage(
            content=answers_result(pending['questions'], answers),
            tool_call_id=pending['call_id'], name=ASK_USER))
    state_file.unlink()
    (directory / LIMIT_FILENAME).unlink(missing_ok=True)
    return {**state, 'history': history, 'checkpoint': messages_from_dict(state['checkpoint'])}


def _completion_written(profile: AgentProfile) -> bool:
    return (profile.completion_file is not None
            and (profile.root / profile.completion_file).is_file())


def configure_container_logging() -> None:
    """Log at the worker's LOG_LEVEL to stderr, each record led by its level name, which
    the worker reads to re-log the container's output at the same level."""
    level = os.getenv('LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(
        level=level if isinstance(logging.getLevelName(level), int) else logging.INFO,
        format='%(levelname)s %(name)s: %(message)s', stream=sys.stderr, force=True,
    )
    for name in ('httpx', 'httpcore', 'openai'):
        logging.getLogger(name).setLevel(logging.WARNING)


async def run_agent(profile: AgentProfile) -> None:
    role = os.getenv('APP_TRACE_AGENT_ROLE', profile.root.name).replace('_', '-')
    outcome = DONE
    # The agent is the container's PID 1, which ignores SIGTERM without a handler: `docker
    # stop` would wait, then kill it with its traces unexported. Cancelling the run instead
    # closes the model's session, which exports it, then flushes below.
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    stopped = []

    def stop() -> None:
        if not stopped and task is not None:
            stopped.append(True)
            task.cancel()

    loop.add_signal_handler(signal.SIGTERM, stop)
    try:
        with trace_operation(f'author-{role}', as_type='agent', feature=role,
                             session_id=os.getenv('APP_TRACE_SESSION_ID'),
                             input=list(profile.initial_messages),
                             trace_context=container_trace_context()) as span:
            outcome = await _run_agent(profile)
            if span is not None:
                span.update(output={
                    PAUSED: 'Paused for the user\'s answers',
                    LIMITED: 'Stopped until the provider is available',
                }.get(outcome, profile.completion_message))
    except asyncio.CancelledError:
        if not stopped:
            raise
        print('Stopped before finishing', flush=True)
        outcome = STOPPED
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        await run_sync(shutdown_tracing)
    if outcome == STOPPED:
        sys.exit(128 + signal.SIGTERM)
    if outcome == PAUSED:
        sys.exit(PAUSE_EXIT)
    if outcome == LIMITED:
        sys.exit(PROVIDER_LIMIT_EXIT)


async def _run_agent(profile: AgentProfile) -> str:
    """Work until the profile's task is done: DONE, PAUSED for the user's answers, or
    LIMITED by the provider's limits."""
    settings = ModelSettings.model_validate_json(
        await run_sync((profile.root.parent / SETTINGS_FILENAME).read_text))
    # The worker sets these from the OpenAI endpoint saved in Settings (openai adapter only).
    provider = create_adapter(settings, api_key=os.getenv('OPENAI_API_KEY', ''),
                              base_url=os.getenv('OPENAI_BASE_URL', ''))
    if settings.adapter == 'claude':
        return await _run_claude(profile, provider)
    return await _run_with_provider(profile, provider, settings.compaction_tokens)


async def _run_claude(profile: AgentProfile, provider) -> str:
    """Let the native SDK execute the stage through MCP and resume its real session."""
    from app.llm.claude import ClaudeTurnLimit

    directory = profile.root.parent
    state_file = directory / STATE_FILENAME
    answers_file = directory / ANSWERS_FILENAME
    state = (
        json.loads(await run_sync(state_file.read_text))
        if await run_sync(state_file.is_file)
        else None
    )
    if state is not None and state.get('adapter') != 'claude':
        raise ValueError('A paused run must resume with the same adapter')
    turns = state['turns'] if state else _max_turns(profile)
    if turns <= 0:
        raise RuntimeError(profile.exhaustion_message)
    pending = {}

    def ask_user(questions: list) -> str:
        pending['questions'] = normalize_questions(questions)
        return 'The run is paused. The user will answer in the next message.'

    tools = [*(await run_sync(workspace_tools, profile.root, profile.contracts, profile.build_dirs)),
             StructuredTool.from_function(ask_user, name=ASK_USER, description=DESCRIPTION,
                                          args_schema=AskUserInput),
             *([note_tool([])] if profile.working_notes else [])]
    provider.cwd = profile.root
    provider.config_dir = directory / '.claude-native'
    provider.resume = state['session_id'] if state else None
    head = [SystemMessage(profile.prompt + ASK_GUIDANCE)]
    if state and state.get('pending') is None:
        # Stopped by the provider's limits: the native session goes on where it stopped.
        await run_sync(state_file.unlink)
        await run_sync((directory / LIMIT_FILENAME).unlink, missing_ok=True)
        if state.get('session_id'):
            messages = [*head, HumanMessage(LIMIT_CONTINUE)]
        else:
            provider.resume = None
            messages = [*head, *(HumanMessage(text) for text in profile.initial_messages)]
        print('Resumed after the provider\'s limits', flush=True)
        state = None
    elif state:
        answers = json.loads(await run_sync(answers_file.read_text))['answers']
        from app.llm.questions import answer_problem
        problem = answer_problem(state['pending']['questions'], answers)
        if problem:
            raise ValueError(problem)
        messages = [*head, HumanMessage(answers_result(state['pending']['questions'], answers))]
        print('Resumed native Claude session with the user\'s answers', flush=True)
    else:
        messages = [*head, *(HumanMessage(text) for text in profile.initial_messages)]
    nudges = state.get('nudges', 0) if state else 0
    try:
        while True:
            if turns <= 0:
                raise RuntimeError(profile.exhaustion_message)
            # The SDK fixes max_turns at client startup. Resume the native transcript
            # in a fresh client for each correction, with only the remaining budget.
            # Counted per session; one that failed to start used none.
            provider.turns_used = 0
            try:
                async with provider.async_session():
                    reply = await provider.ainvoke(
                        messages, tools=tools,
                        until=lambda: bool(pending) or _completion_written(profile),
                        max_turns=turns)
            except Exception as exc:
                if not is_limit_error(exc):
                    raise
                turns -= provider.turns_used
                await run_sync(_stop_for_limit, directory, {
                    'adapter': 'claude', 'session_id': provider.session_id,
                    'turns': turns, 'nudges': nudges}, ProviderLimit(exc))
                return LIMITED
            turns -= provider.turns_used
            if pending:
                await run_sync(
                    _pause,
                    directory,
                    {
                        'adapter': 'claude',
                        'session_id': provider.session_id,
                        'pending': pending,
                        'turns': turns,
                        'nudges': nudges,
                    },
                )
                await run_sync(answers_file.unlink, missing_ok=True)
                print(f'Paused for answers to {len(pending["questions"])} questions', flush=True)
                return PAUSED
            if (not await run_sync(_completion_written, profile)
                    and nudges < DIRECT_QUESTION_NUDGES and asks_directly(reply.text)):
                nudges += 1
                provider.resume = provider.session_id
                messages = [*head, HumanMessage(DIRECT_QUESTION_NOTICE)]
                continue
            await run_sync(profile.validate_completion, profile.root)
            await run_sync(state_file.unlink, missing_ok=True)
            await run_sync(answers_file.unlink, missing_ok=True)
            print(profile.completion_message, flush=True)
            return DONE
    except ClaudeTurnLimit as exc:
        raise RuntimeError(profile.exhaustion_message) from exc


async def _run_with_provider(profile: AgentProfile, provider, compaction_tokens: int) -> str:
    """Run the application's tool loop, compacting its history into a checkpoint once
    `compaction_tokens` are in use; 0 never compacts."""
    working: list[str] = []
    tools = [*(await run_sync(workspace_tools, profile.root, profile.contracts, profile.build_dirs)),
             ask_user_tool(), *([note_tool(working)] if profile.working_notes else [])]
    by_name = {item.name: item for item in tools}
    model = provider.bind_tools(tools)
    checkpoint: list[BaseMessage] = []
    history: list[BaseMessage] = []
    notes = ''
    changed: dict[str, None] = {}
    turns = _max_turns(profile)
    nudges = 0
    resumed = await run_sync(_resume, profile.root.parent)
    if resumed:
        checkpoint, history, notes = resumed['checkpoint'], resumed['history'], resumed['notes']
        changed = dict.fromkeys(resumed['changed'])
        turns, nudges = resumed['turns'], resumed['nudges']
        working += resumed.get('working', [])
        print('Resumed where the run stopped', flush=True)
    # The instructions and authoritative inputs are never compacted.
    head = [SystemMessage(content=profile.prompt + ASK_GUIDANCE + TURN_BUDGET.format(turns=turns)),
            *(HumanMessage(content=text) for text in profile.initial_messages)]
    schemas = len(json.dumps([convert_to_openai_tool(item) for item in tools])) // TOKEN_CHARS
    fixed = schemas + sum(_tokens(message) for message in head)
    # `measured` tokens cover everything sent before history[counted:], which is estimated.
    measured, counted = fixed, 0
    config = langchain_config(run_name='decide-next-action')
    # Disabled tracing leaves the invocation unchanged for custom model adapters.
    traced_config = {'config': config} if 'callbacks' in config else {}
    for turn in range(turns):
        remaining = turns - turn
        notice = []
        if remaining <= FINISHING_TURNS:
            unit = 'turn' if remaining == 1 else 'turns'
            notice = [HumanMessage(content=(
                f'[System] You have {remaining} model {unit} remaining. '
                'Finish the task and write its required output files now. '
                'Do not start new investigations. The run fails if the required outputs '
                'are missing when the turn limit is reached.'))]
        # Keep the current budget outside compacted history and recompute it on resume.
        try:
            response = await _request(lambda: model.ainvoke(
                [*head, *checkpoint, *history, *notice], **traced_config))
        except ProviderLimit as exc:
            await run_sync(_stop_for_limit, profile.root.parent, {
                'history': messages_to_dict(history),
                'checkpoint': messages_to_dict(checkpoint), 'notes': notes,
                'changed': list(changed), 'working': working, 'turns': turns - turn,
                'nudges': nudges}, exc)
            return LIMITED
        history.append(response)
        usage = getattr(response, 'usage_metadata', None) or {}
        if usage.get('input_tokens'):
            measured = usage['input_tokens'] + usage.get('output_tokens', 0)
            counted = len(history)
        if not response.tool_calls:
            if nudges < DIRECT_QUESTION_NUDGES and asks_directly(response.text):
                nudges += 1
                history.append(HumanMessage(content=DIRECT_QUESTION_NOTICE))
                continue
            await run_sync(profile.validate_completion, profile.root)
            print(profile.completion_message, flush=True)
            return DONE
        asked = None
        for call in response.tool_calls:
            if call['name'] == ASK_USER:
                try:
                    if asked:
                        raise ValueError('ask every question in one ask_user call')
                    questions = normalize_questions(call['args'].get('questions'))
                except ValueError as exc:
                    history.append(ToolMessage(content=f'Tool failed: ValueError: {exc}',
                                               tool_call_id=call['id']))
                else:
                    # Its result is the user's answers, added here when the run resumes,
                    # so the results still follow the order of the calls.
                    asked = {'call_id': call['id'], 'questions': questions,
                             'position': len(history)}
                continue
            try:
                result = await by_name[call['name']].ainvoke(
                    call['args'], **({'config': _named(config, call['name'])}
                                    if traced_config else {}),
                )
                if call['name'] in {'write_file', 'edit_file'}:
                    changed[call['args']['path']] = None
            except Exception as exc:
                result = f'Tool failed: {type(exc).__name__}: {exc}'
            history.append(ToolMessage(content=str(result), tool_call_id=call['id']))
        if asked:
            await run_sync(_pause, profile.root.parent, {
                'pending': asked, 'history': messages_to_dict(history),
                'checkpoint': messages_to_dict(checkpoint), 'notes': notes,
                'changed': list(changed), 'working': working, 'turns': turns - turn - 1,
                'nudges': nudges})
            print(f'Paused for answers to {len(asked["questions"])} questions', flush=True)
            return PAUSED
        if await run_sync(_completion_written, profile):
            await run_sync(profile.validate_completion, profile.root)
            print(profile.completion_message, flush=True)
            return DONE
        # There is no following turn to benefit from another compaction request.
        if remaining == 1:
            break
        used = measured + sum(_tokens(message) for message in history[counted:])
        start = _recent_start(history, compaction_tokens * KEEP_RECENT)
        if not compaction_tokens or used <= compaction_tokens or not start:
            continue
        request = [SystemMessage(content=(profile.checkpoint_prompt or CHECKPOINT_PROMPT)
                                 + CLIP_NOTICE), HumanMessage(content=(
            (f'Previous checkpoint:\n{notes}\n\n' if notes else '')
            + 'History being removed:\n' + _transcript(history[:start])))]
        try:
            notes = (await _request(lambda: provider.ainvoke(request, **(
                {'config': {**config, 'run_name': 'compact-history'}} if traced_config else {}
            )))).text.strip()
        except ProviderLimit as exc:
            await run_sync(_stop_for_limit, profile.root.parent, {
                'history': messages_to_dict(history),
                'checkpoint': messages_to_dict(checkpoint), 'notes': notes,
                'changed': list(changed), 'working': working, 'turns': turns - turn - 1,
                'nudges': nudges}, exc)
            return LIMITED
        files = f'\nFiles written or edited so far: {", ".join(changed)}' if changed else ''
        left = f'\nModel turns remaining: {remaining - 1}.'
        checkpoint = [HumanMessage(
            content=f'{CHECKPOINT_NOTICE}{left}{files}{_noted(working)}\n\n{notes}'.strip())]
        del history[:start]
        measured, counted = fixed + _tokens(checkpoint[0]), 0
        print(f'Compacted {start} messages into a checkpoint (about {used} tokens in use)',
              flush=True)
    if not (profile.wrap_up_prompt and profile.completion_file):
        raise RuntimeError(profile.exhaustion_message)
    try:
        await _wrap_up(profile, provider, notes, working, history, traced_config)
    except ProviderLimit as exc:
        # Resumed with no turn left, the run goes straight to its wrap-up again.
        await run_sync(_stop_for_limit, profile.root.parent, {
            'history': messages_to_dict(history), 'checkpoint': messages_to_dict(checkpoint),
            'notes': notes, 'changed': list(changed), 'working': working, 'turns': 0,
            'nudges': nudges}, exc)
        return LIMITED
    return DONE


def _noted(working: Sequence[str]) -> str:
    return ''.join(['\nYour notes so far:', *(f'\n- {item}' for item in working)]
                   if working else [])


async def _wrap_up(profile: AgentProfile, provider, notes: str, working: Sequence[str],
                   history: Sequence[BaseMessage], traced_config: dict[str, Any]) -> None:
    """Write the completion file of a run whose turns ran out from what it established."""
    inputs = '\n\n'.join(profile.initial_messages)
    assert profile.wrap_up_prompt and profile.completion_file  # checked by the caller
    request = [SystemMessage(content=profile.wrap_up_prompt + CLIP_NOTICE), HumanMessage(content=(
        f'Task inputs:\n{inputs}\n{_noted(working)}\n\n'
        + (f'Checkpoint:\n{notes}\n\n' if notes else '')
        + 'Latest history:\n' + _transcript(history)))]
    config = traced_config.get('config')
    reply = await _request(lambda: provider.ainvoke(request, **(
        {'config': {**config, 'run_name': 'wrap-up'}} if config else {})))
    content = re.sub(r'^```\w*\n|\n?```$', '', reply.text.strip())
    target = profile.root / profile.completion_file
    await run_sync(target.write_text, content)
    try:
        await run_sync(profile.validate_completion, profile.root)
    except RuntimeError as exc:
        raise RuntimeError(profile.exhaustion_message) from exc
    print(f'{profile.completion_message} from its notes after the turn limit', flush=True)
