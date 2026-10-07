"""LangChain chat model backed by Claude Code through the Claude Agent SDK."""
import asyncio
import tempfile
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKError, ResultMessage, SessionStore,
    TextBlock, ToolUseBlock, create_sdk_mcp_server, query, tool,
)
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool

# Bound tools reach Claude Code as an in-process MCP server, which prefixes their names.
TOOL_SERVER = 'langchain'
TOOL_PREFIX = f'mcp__{TOOL_SERVER}__'
# Claude Code only starts a turn from a user prompt. A conversation ending in one sends
# it as that prompt; one ending in tool results is resumed whole and nudged with this.
CONTINUE_PROMPT = 'Continue.'


class ChatClaude(BaseChatModel):
    """Each call is one fresh Claude Code process making one model request, with none of
    its built-in tools and none of the user's settings, skills or project context files.
    Credentials come from Claude Code itself: its own login, or `ANTHROPIC_API_KEY` /
    `CLAUDE_CODE_OAUTH_TOKEN` in the environment."""

    # Model ID or alias such as `claude-opus-5-5` or `sonnet`; None uses the CLI default.
    model: str | None = None
    # Claude Code executable; None uses the one bundled with the SDK.
    cli_path: str | None = None
    # Bounds the whole generation: the process is stopped when it runs longer.
    timeout: float = 600

    @property
    def _llm_type(self) -> str:
        return 'claude-agent-sdk'

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any):
        params = super()._get_ls_params(stop=stop, **kwargs)
        params['ls_provider'] = 'anthropic'
        if self.model:
            params['ls_model_name'] = self.model
        return params

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None,
                   **kwargs: Any) -> Runnable:
        # Claude Code cannot force a call, so `tool_choice` is accepted and not honoured.
        return self.bind(tools=[convert_to_openai_tool(item)['function'] for item in tools],
                         **kwargs)

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None,
                  **kwargs: Any) -> ChatResult:
        call = self._agenerate(messages, stop, **kwargs)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(call)
        # A synchronous call made from inside a running loop gets a loop of its own.
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, call).result()

    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                         run_manager: AsyncCallbackManagerForLLMRun | None = None, *,
                         tools: Sequence[dict[str, Any]] = (), **kwargs: Any) -> ChatResult:
        system = '\n\n'.join(message.text for message in messages
                             if isinstance(message, SystemMessage))
        history = _claude_messages(messages)
        prompt = CONTINUE_PROMPT
        if isinstance(history[-1]['content'], str):
            prompt = history.pop()['content']
        session = str(uuid.uuid4())
        blocks: list[Any] = []
        model_name = self.model
        result = None
        # An empty directory of its own: Claude Code describes its working directory to
        # the model and keys sessions by it.
        with tempfile.TemporaryDirectory(prefix='claude-model-') as cwd:
            options = ClaudeAgentOptions(
                model=self.model, cli_path=self.cli_path, cwd=cwd, system_prompt=system,
                # One request per call: the turn limit ends the run before a second one,
                # which hands any tool calls back to the caller.
                max_turns=1, tools=[], setting_sources=[], skills=[], strict_mcp_config=True,
                mcp_servers=({TOOL_SERVER: create_sdk_mcp_server(
                    TOOL_SERVER, tools=[_handed_back(item) for item in tools])}
                    if tools else {}),
                allowed_tools=[TOOL_PREFIX + item['name'] for item in tools],
                # Prompt text is assembled by the caller: no @file or /command expansion.
                verbatim_prompts=True, extra_args={'no-session-persistence': None},
                # The earlier conversation is resumed as a session Claude Code never ran.
                session_store=_History(_transcript(history, session, cwd)) if history else None,
                resume=session if history else None,
            )
            try:
                async with asyncio.timeout(self.timeout):
                    async for event in query(prompt=prompt, options=options):
                        if isinstance(event, ResultMessage):
                            result = event
                        elif (isinstance(event, AssistantMessage)
                              and event.parent_tool_use_id is None):
                            # One response arrives as a message per content block.
                            blocks += event.content
                            model_name = event.model
            except TimeoutError as exc:
                raise TimeoutError(
                    f'claude did not finish within {self.timeout:g} seconds') from exc
            except ClaudeSDKError:
                # The CLI exits non-zero after any error result, the turn limit included.
                if result is None:
                    raise
        if result is None:
            raise RuntimeError('claude produced no result')
        calls = [block for block in blocks if isinstance(block, ToolUseBlock)]
        if result.is_error and not (calls and result.subtype == 'error_max_turns'):
            raise RuntimeError('claude response failed: '
                               + ('; '.join(result.errors or []) or result.result
                                  or result.subtype))
        usage = result.usage or {}
        cached = {'cache_read': usage.get('cache_read_input_tokens') or 0,
                  'cache_creation': usage.get('cache_creation_input_tokens') or 0}
        prompt_tokens = (usage.get('input_tokens') or 0) + sum(cached.values())
        output_tokens = usage.get('output_tokens') or 0
        message = AIMessage(
            content=''.join(block.text for block in blocks if isinstance(block, TextBlock)),
            tool_calls=[{'name': block.name.removeprefix(TOOL_PREFIX), 'args': block.input,
                         'id': block.id, 'type': 'tool_call'} for block in calls],
            usage_metadata={'input_tokens': prompt_tokens, 'output_tokens': output_tokens,
                            'total_tokens': prompt_tokens + output_tokens,
                            'input_token_details': cached},
            response_metadata={'model_name': model_name, 'finish_reason': result.stop_reason,
                               'total_cost_usd': result.total_cost_usd},
        )
        return ChatResult(generations=[ChatGeneration(message=message)],
                          llm_output={'model_name': model_name})


class _History(SessionStore):
    """Serves one prepared transcript; nothing Claude Code writes is kept."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries

    async def load(self, key: Any) -> list[dict[str, Any]]:
        return self.entries

    async def append(self, key: Any, entries: list[Any]) -> None:
        pass


def _handed_back(spec: dict[str, Any]):
    """Declares a bound tool to Claude Code. The turn limit stops the run before this
    empty result reaches the model: the caller - not Claude Code - does the work."""
    @tool(spec['name'], spec.get('description', ''), spec['parameters'])
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return {'content': [{'type': 'text', 'text': ''}]}

    return handler


def _claude_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """The conversation as Anthropic messages; system messages travel separately. User
    text stays a string, which tells it apart from a list of tool results."""
    converted: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, AIMessage):
            content = [*([{'type': 'text', 'text': message.text}] if message.text else []),
                       *({'type': 'tool_use', 'id': call['id'],
                          'name': TOOL_PREFIX + call['name'], 'input': call['args']}
                         for call in message.tool_calls)]
            # The API rejects an assistant message with nothing in it.
            if content:
                converted.append({'role': 'assistant', 'content': content})
        elif isinstance(message, ToolMessage):
            result = {'type': 'tool_result', 'tool_use_id': message.tool_call_id,
                      'content': message.text, 'is_error': message.status == 'error'}
            # Every result of one assistant message belongs to the same user message.
            if converted and isinstance(converted[-1]['content'], list) \
                    and converted[-1]['role'] == 'user':
                converted[-1]['content'].append(result)
            else:
                converted.append({'role': 'user', 'content': [result]})
        else:
            converted.append({'role': 'user', 'content': message.text})
    if not converted:
        raise ValueError('claude needs at least one non-system message')
    return converted


def _transcript(messages: list[dict[str, Any]], session: str, cwd: str) -> list[dict[str, Any]]:
    """The messages as the session transcript entries Claude Code resumes from."""
    timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    entries: list[dict[str, Any]] = []
    for message in messages:
        if message['role'] == 'assistant':
            calls = any(block['type'] == 'tool_use' for block in message['content'])
            # This origin names no real model, so nothing is assumed about the turn.
            message = {**message, 'id': f'msg_{uuid.uuid4().hex}', 'type': 'message',
                       'model': 'langchain', 'stop_reason': 'tool_use' if calls else 'end_turn',
                       'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}
        entries.append({
            'parentUuid': entries[-1]['uuid'] if entries else None, 'isSidechain': False,
            'type': message['role'], 'message': message, 'uuid': str(uuid.uuid4()),
            'timestamp': timestamp.replace('+00:00', 'Z'), 'userType': 'external',
            'cwd': cwd, 'sessionId': session})
    return entries
