"""LangChain chat model backed by the Pi coding-agent CLI (`pi --mode json`)."""
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool

# Registers the bound tools and swaps the caller's history in as native messages.
EXTENSION = Path(__file__).with_name('pi_extension.ts')
INPUT_ENV = 'PI_LANGCHAIN_INPUT'
# Pi's own override for its configuration directory.
AGENT_DIR_ENV = 'PI_CODING_AGENT_DIR'
# Pi runs as a bare model: no saved session, none of its built-in tools and none of the
# user's local extensions, skills, templates or project context files.
PI_FLAGS = ('--mode', 'json', '--no-session', '--no-extensions', '--no-skills',
            '--no-prompt-templates', '--no-context-files', '--no-themes', '--no-approve',
            '--extension', str(EXTENSION))
# Pi only starts a run from a prompt; the extension replaces it with the real history.
PLACEHOLDER_PROMPT = 'continue'
NO_USAGE = {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'totalTokens': 0,
            'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'total': 0}}
MAX_ERROR_CHARS = 2000


class ChatPi(BaseChatModel):
    """Each call is one fresh `pi` process making one model request. Credentials and the
    default model come from Pi's own configuration (`agent_dir`, else `~/.pi/agent`)."""

    # Pi model pattern such as `openai/gpt-4o` or `sonnet:high`; None uses Pi's default.
    model: str | None = None
    thinking_level: str = 'default'
    command: str = 'pi'
    # Pi configuration directory; None leaves Pi to its environment and default.
    agent_dir: str | None = None
    # Bounds the whole generation: the process is killed when it runs longer.
    timeout: float = 600

    @property
    def _llm_type(self) -> str:
        return 'pi'

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None,
                   **kwargs: Any) -> Runnable:
        # Pi cannot force a call, so `tool_choice` is accepted and not honoured.
        return self.bind(tools=[convert_to_openai_tool(item)['function'] for item in tools],
                         **kwargs)

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None, *,
                  tools: Sequence[dict[str, Any]] = (), **kwargs: Any) -> ChatResult:
        system = '\n\n'.join(message.text for message in messages
                             if isinstance(message, SystemMessage))
        command = [self.command, *PI_FLAGS]
        # Naming the bound tools keeps Pi's own read/bash/edit/write tools switched off.
        command += (['--tools', ','.join(tool['name'] for tool in tools)] if tools
                    else ['--no-tools'])
        if self.model:
            command += ['--model', self.model]
        if self.thinking_level != 'default':
            command += ['--thinking', self.thinking_level]
        if system:
            command += ['--system-prompt', system]
        env = {**os.environ, AGENT_DIR_ENV: self.agent_dir} if self.agent_dir else os.environ
        with tempfile.NamedTemporaryFile('w', suffix='.json') as request:
            json.dump({'tools': list(tools), 'messages': _pi_messages(messages)}, request)
            request.flush()
            try:
                process = subprocess.run(
                    command, input=PLACEHOLDER_PROMPT, capture_output=True, text=True,
                    timeout=self.timeout, env={**env, INPUT_ENV: request.name})
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(
                    f'pi did not finish within {self.timeout:g} seconds') from exc
        if process.returncode:
            raise RuntimeError(f'pi exited with code {process.returncode}: '
                               f'{process.stderr.strip()[-MAX_ERROR_CHARS:]}')
        reply = _final_reply(process.stdout)
        # JSON mode reports a failed response in the stream, not in the exit status.
        if reply.get('stopReason') in {'error', 'aborted'}:
            raise RuntimeError(f'pi response failed: '
                               f'{reply.get("errorMessage") or reply["stopReason"]}')
        content = reply['content']
        usage = reply.get('usage') or {}
        prompt_tokens = sum(usage.get(key, 0) for key in ('input', 'cacheRead', 'cacheWrite'))
        message = AIMessage(
            content=''.join(block['text'] for block in content if block['type'] == 'text'),
            tool_calls=[{'name': block['name'], 'args': block['arguments'], 'id': block['id'],
                         'type': 'tool_call'}
                        for block in content if block['type'] == 'toolCall'],
            usage_metadata={'input_tokens': prompt_tokens,
                            'output_tokens': usage.get('output', 0),
                            'total_tokens': prompt_tokens + usage.get('output', 0)},
            response_metadata={'model_name': reply.get('model'),
                               'model_provider': reply.get('provider'),
                               'finish_reason': reply.get('stopReason')},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _pi_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """The conversation in Pi's message format; system messages travel separately."""
    timestamp = int(time.time() * 1000)
    tool_names: dict[str, str] = {}
    converted: list[dict[str, Any]] = []
    for message in messages:
        text = [{'type': 'text', 'text': message.text}] if message.text else []
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, AIMessage):
            tool_names.update((call['id'], call['name']) for call in message.tool_calls)
            calls = [{'type': 'toolCall', 'id': call['id'], 'name': call['name'],
                      'arguments': call['args']} for call in message.tool_calls]
            # Pi needs an origin for every assistant message; this one names no real
            # provider, so Pi treats the turn as coming from a different model.
            converted.append({
                'role': 'assistant', 'content': [*text, *calls], 'api': 'langchain',
                'provider': 'langchain', 'model': 'unknown', 'usage': NO_USAGE,
                'stopReason': 'toolUse' if calls else 'stop', 'timestamp': timestamp})
        elif isinstance(message, ToolMessage):
            converted.append({
                'role': 'toolResult', 'toolCallId': message.tool_call_id,
                'toolName': message.name or tool_names.get(message.tool_call_id, 'tool'),
                'content': text or [{'type': 'text', 'text': ''}],
                'isError': message.status == 'error', 'timestamp': timestamp})
        else:
            converted.append({'role': 'user', 'content': text, 'timestamp': timestamp})
    if not converted:
        raise ValueError('pi needs at least one non-system message')
    return converted


def _final_reply(stdout: str) -> dict[str, Any]:
    """The assistant message to return: the first one asking for tools, as whatever follows
    it is only the extension stopping the run; otherwise the last one, since earlier
    messages are attempts Pi retried."""
    reply = None
    # Records are split on LF only: other Unicode line separators may occur inside strings.
    for line in stdout.split('\n'):
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get('type') == 'message_end' and event['message'].get('role') == 'assistant':
            reply = event['message']
            if reply.get('stopReason') == 'toolUse':
                break
    if reply is None:
        raise RuntimeError('pi produced no assistant response')
    return reply
