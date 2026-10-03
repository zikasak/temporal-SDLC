"""Container-only tools and bounded model loop. No host config, Temporal or Docker."""
import os
import signal
import subprocess
import tempfile
from collections.abc import Callable, Sequence
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


def workspace_tools(root: Path) -> list[BaseTool]:
    root = root.resolve()

    def workspace_path(path: str) -> Path:
        result = (root / path).resolve()
        if not result.is_relative_to(root):
            raise ValueError('Path must be inside the project workspace')
        return result

    @tool
    def read_file(path: str) -> str:
        """Read a UTF-8 file relative to the project root."""
        return workspace_path(path).read_text()[:30000]

    @tool
    def write_file(path: str, content: str) -> str:
        """Create or replace a file relative to the project root."""
        target = workspace_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return 'Written'

    @tool
    def shell(command: str) -> str:
        """Run a shell command in the project (120 second timeout)."""
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(command, shell=True, cwd=root, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                process.wait(timeout=120)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                return 'Command timed out'
            output.seek(0, 2)
            output.seek(max(0, output.tell() - 16000))
            return f'Exit code: {process.returncode}\n' + output.read().decode('utf-8', 'replace')

    return [read_file, write_file, shell]


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
