"""Claude native request behavior and isolated-agent credential boundaries."""
import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app import docker_agents
from app.llm.claude import ChatClaude


@pytest.mark.parametrize('thinking_level', ['default', 'off', 'high'])
async def test_native_reply_and_thinking_options(claude_http, thinking_level):
    replies, requests = claude_http
    replies.append([{'type': 'text', 'text': 'A complete '},
                    {'type': 'text', 'text': 'reply.'}])
    reply = await ChatClaude(model='claude-opus-5-5', thinking_level=thinking_level,
                             timeout=10).ainvoke([
        SystemMessage('Be brief.'), HumanMessage('@notes.md /help question')])
    assert reply.text == 'A complete reply.'
    assert not reply.tool_calls
    assert reply.response_metadata['session_id']
    assert reply.response_metadata['model_name'] == 'claude-opus-5-5'
    [request] = requests
    assert request['messages'][0]['content'] == '@notes.md /help question'
    assert not request.get('tools')
    if thinking_level == 'high':
        assert request['thinking']['type'] == 'adaptive'
        assert request['output_config']['effort'] == 'high'
    elif thinking_level == 'off':
        assert request.get('thinking', {}).get('type', 'disabled') == 'disabled'


async def test_claude_harness_forwards_credentials_by_name(created_container, monkeypatch, saved_model_settings):
    from app.settings.models import ModelSettings
    saved_model_settings['implementation'] = ModelSettings(
        adapter='claude', model='claude-test', thinking_level='high')
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'oauth-secret')
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'http://localhost:4000')
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.delenv('ANTHROPIC_AUTH_TOKEN', raising=False)

    args, settings = await created_container()

    variables = [args[index + 1] for index, arg in enumerate(args) if arg == '-e']
    assert {'CLAUDE_CODE_OAUTH_TOKEN',
            'ANTHROPIC_BASE_URL=http://host.docker.internal:4000'} <= set(variables)
    assert 'ANTHROPIC_API_KEY' not in variables
    # The secret travels in docker's environment, never on its command line.
    assert not any('oauth-secret' in arg for arg in args)
    assert settings == saved_model_settings['implementation']


async def test_claude_harness_requires_credentials(created_container, monkeypatch, saved_model_settings):
    from app.settings.models import ModelSettings
    saved_model_settings['implementation'] = ModelSettings(adapter='claude', model='sonnet')
    for name in docker_agents.CLAUDE_CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match='Claude credentials not found'):
        await created_container()
