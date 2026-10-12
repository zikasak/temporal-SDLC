"""Drive the Claude adapter through the real Claude Agent SDK and a stand-in `claude` CLI."""
from functools import partial

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from app import docker_agents
from app.agent_workflows import runtime
from app.implementation.agent import validate_completion
from app.llm import claude
from app.llm.claude import ChatClaude
from tests.conftest import claude_reply


def flag(argv, name):
    return argv[argv.index(name) + 1]


def test_returns_final_text_and_usage(fake_claude):
    install, calls = fake_claude
    model = ChatClaude(model='claude-test', cli_path=install(claude_reply('line one', ' line two')))

    reply = model.invoke([SystemMessage('Be brief.'), HumanMessage('@notes.md /help question')])

    assert reply.content == 'line one line two'
    assert reply.tool_calls == []
    assert reply.usage_metadata == {
        'input_tokens': 30, 'output_tokens': 5, 'total_tokens': 35,
        'input_token_details': {'cache_read': 7, 'cache_creation': 3}}
    assert reply.response_metadata['model_name'] == 'claude-test'
    [call] = calls()
    argv = call['argv']
    assert flag(argv, '--system-prompt') == 'Be brief.'
    assert flag(argv, '--model') == 'claude-test'
    # A bare model: one request, no built-in tools, no user or project settings.
    assert (flag(argv, '--max-turns'), flag(argv, '--tools')) == ('1', '')
    assert '--setting-sources=' in argv
    assert not any(arg.startswith('--resume') for arg in argv) and call['tools'] == []
    assert call['prompt']['message'] == {'role': 'user', 'content': '@notes.md /help question'}
    # Without this mark Claude Code would read the mentioned file and run the command.
    assert call['prompt']['client_composed'] is True


def test_history_is_resumed_as_native_messages(fake_claude):
    install, calls = fake_claude
    model = ChatClaude(cli_path=install(claude_reply('ok'), claude_reply('ok')))
    history = [
        SystemMessage('Be brief.'),
        HumanMessage('Weather in Paris?'),
        AIMessage('Checking.', tool_calls=[
            {'name': 'get_weather', 'args': {'city': 'Paris'}, 'id': 'call-1'},
            {'name': 'get_time', 'args': {'city': 'Paris'}, 'id': 'call-2'}]),
        ToolMessage('17C', tool_call_id='call-1'),
        ToolMessage('unknown city', tool_call_id='call-2', status='error'),
    ]

    model.invoke(history)
    model.invoke([*history, AIMessage('17C.'), HumanMessage('And tomorrow?')])

    after_tools, after_question = calls()
    assert '--model' not in after_tools['argv']
    user, asked, results = after_tools['transcript']
    assert (user['role'], user['content']) == ('user', 'Weather in Paris?')
    assert asked['role'] == 'assistant' and asked['stop_reason'] == 'tool_use'
    assert asked['content'] == [
        {'type': 'text', 'text': 'Checking.'},
        {'type': 'tool_use', 'id': 'call-1', 'name': 'mcp__langchain__get_weather',
         'input': {'city': 'Paris'}},
        {'type': 'tool_use', 'id': 'call-2', 'name': 'mcp__langchain__get_time',
         'input': {'city': 'Paris'}}]
    # Both results answer one assistant message, so they share one user message.
    assert (results['role'], results['content']) == ('user', [
        {'type': 'tool_result', 'tool_use_id': 'call-1', 'content': '17C', 'is_error': False},
        {'type': 'tool_result', 'tool_use_id': 'call-2', 'content': 'unknown city',
         'is_error': True}])
    # Tool results cannot start a turn; the resumed conversation is only nudged on.
    assert after_tools['prompt']['message']['content'] == claude.CONTINUE_PROMPT
    # A closing user message is the prompt itself and stays out of the resumed history.
    assert after_question['prompt']['message']['content'] == 'And tomorrow?'
    assert [(item['role'], item['content']) for item in after_question['transcript'][3:]] == [
        ('assistant', [{'type': 'text', 'text': '17C.'}])]


def test_bound_tools_are_declared_and_their_calls_returned(fake_claude):
    install, calls = fake_claude

    @tool
    def lookup(term: str, limit: int = 5) -> str:
        """Look a term up."""
        return term

    model = ChatClaude(cli_path=install(claude_reply(
        'Looking.', ('call-9', 'mcp__langchain__lookup', {'term': 'pi'}),
    ))).bind_tools([lookup], tool_choice='auto')

    reply = model.invoke('look up pi')

    assert reply.content == 'Looking.'
    assert reply.tool_calls == [
        {'name': 'lookup', 'args': {'term': 'pi'}, 'id': 'call-9', 'type': 'tool_call'}]
    assert reply.response_metadata['finish_reason'] == 'tool_use'
    [call] = calls()
    # Unless allowed, Claude Code would stop to ask permission for the call.
    assert flag(call['argv'], '--allowedTools') == 'mcp__langchain__lookup'
    [declared] = call['tools']
    assert declared['name'] == 'lookup' and declared['description'] == 'Look a term up.'
    assert declared['inputSchema']['required'] == ['term']
    assert set(declared['inputSchema']['properties']) == {'term', 'limit'}


AFTER_TOOLS = [HumanMessage('Weather in Paris?'),
               AIMessage('', tool_calls=[
                   {'name': 'get_weather', 'args': {'city': 'Paris'}, 'id': 'call-1'}]),
               ToolMessage('17C', tool_call_id='call-1')]


async def test_repeated_resume_filler_is_asked_for_once_more(fake_claude):
    install, calls = fake_claude
    model = ChatClaude(cli_path=install(
        claude_reply('No response requested.'), claude_reply('17C.')))

    reply = await model.ainvoke(AFTER_TOOLS)

    assert reply.content == '17C.'
    first, second = calls()
    # The second run is the same request again.
    assert [(item['role'], item['content']) for item in first['transcript']] == [
        (item['role'], item['content']) for item in second['transcript']]
    assert first['prompt']['message'] == second['prompt']['message']


async def test_resume_filler_repeated_twice_raises(fake_claude):
    install, _ = fake_claude
    filler = claude_reply('No response requested.')
    with pytest.raises(RuntimeError, match='resume filler'):
        await ChatClaude(cli_path=install(filler, filler)).ainvoke(AFTER_TOOLS)


async def test_failed_response_raises(fake_claude):
    install, _ = fake_claude
    model = ChatClaude(cli_path=install(claude_reply(
        'API Error: 401', is_error=True, result='API Error: 401 invalid x-api-key')))
    with pytest.raises(RuntimeError, match='401 invalid x-api-key'):
        await model.ainvoke('hi')


def test_coding_agent_runs_its_tools_through_claude(fake_claude, tmp_path, monkeypatch):
    install, calls = fake_claude
    cli = install(
        claude_reply(('call-1', 'mcp__langchain__write_file',
                      {'path': 'Dockerfile', 'content': 'FROM scratch'})),
        claude_reply('Finished'))
    monkeypatch.setattr(claude, 'ChatClaude', partial(ChatClaude, cli_path=cli))
    monkeypatch.setenv('LLM_HARNESS', 'claude')
    monkeypatch.setenv('CLAUDE_MODEL', 'claude-test')
    for name in ('OPENAI_MODEL', 'OPENAI_API_KEY', 'OPENAI_BASE_URL'):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / 'service'
    root.mkdir()

    runtime.run_agent(runtime.AgentProfile(
        root=root, prompt='Implement the supplied contract', initial_messages=['requirements'],
        max_turns_env='TEST_AGENT_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Completed', exhaustion_message='Tool-turn limit exceeded',
    ))

    assert (root / 'Dockerfile').read_text() == 'FROM scratch'
    first, second = calls()
    assert flag(first['argv'], '--model') == 'claude-test'
    assert 'write_file' in [item['name'] for item in first['tools']]
    assert second['transcript'][-1]['content'] == [
        {'type': 'tool_result', 'tool_use_id': 'call-1', 'content': 'Written',
         'is_error': False}]


async def test_claude_harness_forwards_credentials_by_name(created_container, monkeypatch):
    monkeypatch.setattr(docker_agents, 'LLM_HARNESS', 'claude')
    monkeypatch.setattr(docker_agents, 'CLAUDE_MODEL', 'claude-test')
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'oauth-secret')
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'http://localhost:4000')
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.delenv('ANTHROPIC_AUTH_TOKEN', raising=False)

    args = await created_container()

    variables = [args[index + 1] for index, arg in enumerate(args) if arg == '-e']
    assert {'LLM_HARNESS=claude', 'CLAUDE_MODEL=claude-test', 'CLAUDE_CODE_OAUTH_TOKEN',
            'ANTHROPIC_BASE_URL=http://host.docker.internal:4000'} <= set(variables)
    assert 'ANTHROPIC_API_KEY' not in variables
    # The secret travels in docker's environment, never on its command line.
    assert not any('oauth-secret' in arg for arg in args)


async def test_claude_harness_requires_credentials(created_container, monkeypatch):
    monkeypatch.setattr(docker_agents, 'LLM_HARNESS', 'claude')
    for name in docker_agents.CLAUDE_CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match='Claude credentials not found'):
        await created_container()
