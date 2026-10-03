"""Container-only tools and bounded model loop. No host config, Temporal or Docker."""
import fnmatch
import itertools
import os
import re
import signal
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool

from app.llm.factory import create_model
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
SKIPPED_DIRS = {OUTPUT_DIR, '.git', 'target', 'node_modules', '__pycache__'}
DIAGNOSTIC = re.compile(r'\[ERROR\]|\berror\b|BUILD (SUCCESS|FAILURE)|Exception|FAILED|Tests run:')


def workspace_tools(root: Path) -> list[BaseTool]:
    root = root.resolve()
    commands = itertools.count(1)

    def workspace_path(path: str) -> Path:
        result = (root / path).resolve()
        if not result.is_relative_to(root):
            raise ValueError('Path must be inside the project workspace')
        return result

    def workspace_files(start: Path, pattern: str | None = None) -> Iterator[Path]:
        """Regular files in stable order, never links, build output or saved logs."""
        if start.is_file():
            yield start
            return
        for directory, names, files in os.walk(start):
            names[:] = sorted(name for name in names if name not in SKIPPED_DIRS)
            for name in sorted(files):
                item = Path(directory, name)
                relative = item.relative_to(root).as_posix()
                if item.is_symlink() or (pattern and not (fnmatch.fnmatch(relative, pattern)
                                                          or fnmatch.fnmatch(name, pattern))):
                    continue
                yield item

    @tool
    def read_file(path: str, offset: int = 1, limit: int = READ_LINES) -> str:
        """Read a UTF-8 file relative to the project root.

        Returns up to `limit` lines starting at 1-based line `offset`. A final note
        says how to continue when more of the file remains.
        """
        lines = workspace_path(path).read_text().splitlines(keepends=True)
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
        expression = re.compile(regex)
        matches = []
        for item in workspace_files(workspace_path(path), pattern):
            try:
                lines = item.read_text().splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(lines, 1):
                if expression.search(line):
                    if len(matches) == SEARCH_MATCHES:
                        matches.append(f'[More than {SEARCH_MATCHES} matches; narrow the search.]')
                        return '\n'.join(matches)
                    matches.append(f'{item.relative_to(root).as_posix()}:{number}: '
                                   f'{line.strip()[:LINE_CHARS]}')
        return '\n'.join(matches) or 'No matches'

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

    return [read_file, write_file, edit_file, list_files, search_files, shell]


def run_agent(profile: AgentProfile) -> None:
    role = os.getenv('APP_TRACE_AGENT_ROLE', profile.root.name).replace('_', '-')
    try:
        with trace_operation(f'author-{role}', as_type='agent', feature=role,
                             session_id=os.getenv('APP_TRACE_SESSION_ID'),
                             input=list(profile.initial_messages),
                             trace_context=container_trace_context()) as span:
            _run_agent(profile)
            if span is not None:
                span.update(output=profile.completion_message)
    finally:
        shutdown_tracing()


def _run_agent(profile: AgentProfile) -> None:
    model = create_model(model_name=os.environ['OPENAI_MODEL'],
                         api_key=os.environ['OPENAI_API_KEY'],
                         base_url=os.environ['OPENAI_BASE_URL'])
    tools = workspace_tools(profile.root)
    by_name = {item.name: item for item in tools}
    model = model.bind_tools(tools)
    messages = [SystemMessage(content=profile.prompt),
                *(HumanMessage(content=text) for text in profile.initial_messages)]
    config = langchain_config(run_name='decide-next-action')
    # Disabled tracing leaves the invocation unchanged for custom model adapters.
    traced_config = {'config': config} if 'callbacks' in config else {}
    for _ in range(int(os.getenv(profile.max_turns_env, str(profile.default_max_turns)))):
        response = model.invoke(messages, **traced_config)
        messages.append(response)
        if not response.tool_calls:
            profile.validate_completion(profile.root)
            print(profile.completion_message, flush=True)
            return
        for call in response.tool_calls:
            try:
                result = by_name[call['name']].invoke(
                    call['args'], **({'config': {**config, 'run_name': call['name']}}
                                    if traced_config else {}),
                )
            except Exception as exc:
                result = f'Tool failed: {type(exc).__name__}: {exc}'
            messages.append(ToolMessage(content=str(result), tool_call_id=call['id']))
    raise RuntimeError(profile.exhaustion_message)
