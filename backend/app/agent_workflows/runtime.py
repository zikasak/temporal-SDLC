"""Container-only tools and bounded model loop. No host config, Temporal or Docker."""
import fnmatch
import itertools
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, messages_from_dict,
    messages_to_dict,
)
from langchain_core.tools import BaseTool, StructuredTool, tool
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.llm.factory import create_model
from app.llm.questions import (
    ASK_USER, DESCRIPTION, AskUserInput, answers_result, asks_directly, normalize_questions,
)
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
    default_max_turns: int = 80
    # Read-only contract files supplied instead of embedding large contracts in full.
    contracts: Path | None = None
    # Directory names holding the language's build output and installed dependencies.
    build_dirs: Sequence[str] = ()


READ_LINES = 500
READ_CHARS = 30000
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
# Size of the model's context window in tokens; 0 disables history compaction.
CONTEXT_TOKENS_ENV = 'AGENT_CONTEXT_TOKENS'
DEFAULT_CONTEXT_TOKENS = 64000
# Compacting at this share of the window leaves room for the reply and estimate error.
COMPACT_AT = 0.75
KEEP_RECENT = 0.25
# Code and JSON run near three characters a token; used when the provider reports no usage.
TOKEN_CHARS = 3
CLIP_CHARS = 1500
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
    def shell(command: str) -> str:
        """Run a shell command in the project (120 second timeout).

        Long output is saved to a log file and summarized as its error lines and tail;
        the summary names the log so read_file or search_files can retrieve the rest.
        """
        logs = root / OUTPUT_DIR
        logs.mkdir(exist_ok=True)
        log = logs / f'shell-{next(commands)}.log'
        with log.open('wb') as output:
            process = subprocess.Popen(command, shell=True, cwd=root, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                process.wait(timeout=120)
                status = f'Exit code: {process.returncode}'
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                status = 'Command timed out'
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

    return [*tools, read_contract, search_contracts]


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
    return f'{text[:half]}\n[{len(text) - CLIP_CHARS} characters omitted]\n{text[-half:]}'


def _transcript(messages: Sequence[BaseMessage]) -> str:
    lines = []
    for message in messages:
        if isinstance(message, ToolMessage):
            lines.append(f'Result: {_clip(str(message.content))}')
            continue
        if message.text:
            lines.append(f'Agent: {_clip(message.text)}')
        lines += [f'Call {call["name"]}: {_clip(json.dumps(call["args"], ensure_ascii=False))}'
                  for call in message.tool_calls]
    return '\n'.join(lines)


def ask_user_tool() -> BaseTool:
    """Declared to the model only: the loop pauses the run instead of calling it."""
    def ask_user(questions: list) -> str:
        raise RuntimeError('ask_user pauses the run')

    return StructuredTool.from_function(ask_user, name=ASK_USER, description=DESCRIPTION,
                                        args_schema=AskUserInput)


def _pause(directory: Path, state: dict[str, Any]) -> None:
    """Save where the run stopped, for the run that continues it once the user answers."""
    temporary = directory / f'{STATE_FILENAME}.tmp'
    temporary.write_text(json.dumps(state, ensure_ascii=False))
    os.replace(temporary, directory / STATE_FILENAME)


def _resume(directory: Path) -> dict[str, Any] | None:
    """The saved state of a paused run, its ask_user call answered; None for a new run."""
    state_file, answers_file = directory / STATE_FILENAME, directory / ANSWERS_FILENAME
    if not state_file.is_file():
        return None
    state = json.loads(state_file.read_text())
    answers = json.loads(answers_file.read_text())['answers']
    state_file.unlink()
    answers_file.unlink()
    pending = state['pending']
    history = messages_from_dict(state['history'])
    history.insert(pending['position'], ToolMessage(
        content=answers_result(pending['questions'], answers),
        tool_call_id=pending['call_id'], name=ASK_USER))
    return {**state, 'history': history, 'checkpoint': messages_from_dict(state['checkpoint'])}


def run_agent(profile: AgentProfile) -> None:
    role = os.getenv('APP_TRACE_AGENT_ROLE', profile.root.name).replace('_', '-')
    paused = False
    try:
        with trace_operation(f'author-{role}', as_type='agent', feature=role,
                             session_id=os.getenv('APP_TRACE_SESSION_ID'),
                             input=list(profile.initial_messages),
                             trace_context=container_trace_context()) as span:
            paused = _run_agent(profile)
            if span is not None:
                span.update(output='Paused for the user\'s answers' if paused
                            else profile.completion_message)
    finally:
        shutdown_tracing()
    if paused:
        sys.exit(PAUSE_EXIT)


def _run_agent(profile: AgentProfile) -> bool:
    """Work until the profile's task is done; True when the run paused for answers."""
    harness = os.getenv('LLM_HARNESS', '').lower()
    if harness == 'pi':
        from app.llm.pi import ChatPi

        provider = ChatPi(model=os.getenv('PI_MODEL') or None)
    elif harness == 'claude':
        from app.llm.claude import ChatClaude

        provider = ChatClaude(model=os.getenv('CLAUDE_MODEL') or None)
    else:
        provider = create_model(model_name=os.environ['OPENAI_MODEL'],
                                api_key=os.environ['OPENAI_API_KEY'],
                                base_url=os.environ['OPENAI_BASE_URL'])
    tools = [*workspace_tools(profile.root, profile.contracts, profile.build_dirs),
             ask_user_tool()]
    by_name = {item.name: item for item in tools}
    model = provider.bind_tools(tools)
    # The instructions and authoritative inputs are never compacted.
    head = [SystemMessage(content=profile.prompt + ASK_GUIDANCE),
            *(HumanMessage(content=text) for text in profile.initial_messages)]
    checkpoint: list[BaseMessage] = []
    history: list[BaseMessage] = []
    notes = ''
    changed: dict[str, None] = {}
    turns = int(os.getenv(profile.max_turns_env, str(profile.default_max_turns)))
    nudges = 0
    resumed = _resume(profile.root.parent)
    if resumed:
        checkpoint, history, notes = resumed['checkpoint'], resumed['history'], resumed['notes']
        changed = dict.fromkeys(resumed['changed'])
        turns, nudges = resumed['turns'], resumed['nudges']
        print('Resumed with the user\'s answers', flush=True)
    budget = int(os.getenv(CONTEXT_TOKENS_ENV, str(DEFAULT_CONTEXT_TOKENS)))
    schemas = len(json.dumps([convert_to_openai_tool(item) for item in tools])) // TOKEN_CHARS
    fixed = schemas + sum(_tokens(message) for message in head)
    # `measured` tokens cover everything sent before history[counted:], which is estimated.
    measured, counted = fixed, 0
    config = langchain_config(run_name='decide-next-action')
    # Disabled tracing leaves the invocation unchanged for custom model adapters.
    traced_config = {'config': config} if 'callbacks' in config else {}
    for turn in range(turns):
        response = model.invoke([*head, *checkpoint, *history], **traced_config)
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
            profile.validate_completion(profile.root)
            print(profile.completion_message, flush=True)
            return False
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
                result = by_name[call['name']].invoke(
                    call['args'], **({'config': {**config, 'run_name': call['name']}}
                                    if traced_config else {}),
                )
                if call['name'] in {'write_file', 'edit_file'}:
                    changed[call['args']['path']] = None
            except Exception as exc:
                result = f'Tool failed: {type(exc).__name__}: {exc}'
            history.append(ToolMessage(content=str(result), tool_call_id=call['id']))
        if asked:
            _pause(profile.root.parent, {
                'pending': asked, 'history': messages_to_dict(history),
                'checkpoint': messages_to_dict(checkpoint), 'notes': notes,
                'changed': list(changed), 'turns': turns - turn - 1, 'nudges': nudges})
            print(f'Paused for answers to {len(asked["questions"])} questions', flush=True)
            return True
        used = measured + sum(_tokens(message) for message in history[counted:])
        start = _recent_start(history, budget * KEEP_RECENT)
        if not budget or used <= budget * COMPACT_AT or not start:
            continue
        request = [SystemMessage(content=CHECKPOINT_PROMPT), HumanMessage(content=(
            (f'Previous checkpoint:\n{notes}\n\n' if notes else '')
            + 'History being removed:\n' + _transcript(history[:start])))]
        notes = provider.invoke(request, **(
            {'config': {**config, 'run_name': 'compact-history'}} if traced_config else {}
        )).text.strip()
        files = f'\nFiles written or edited so far: {", ".join(changed)}' if changed else ''
        checkpoint = [HumanMessage(content=f'{CHECKPOINT_NOTICE}{files}\n\n{notes}'.strip())]
        del history[:start]
        measured, counted = fixed + _tokens(checkpoint[0]), 0
        print(f'Compacted {start} messages into a checkpoint (about {used} tokens in use)',
              flush=True)
    raise RuntimeError(profile.exhaustion_message)
