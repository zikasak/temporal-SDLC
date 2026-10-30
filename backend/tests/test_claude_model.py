"""Claude native request behavior and isolated-agent credential boundaries."""
import os
import shlex
import shutil
from pathlib import Path

import claude_agent_sdk
import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app import docker_agents
from app.llm.claude import ChatClaude


# The CLI always sends adaptive thinking to Opus 5 models, so 'off' is checked on a
# model that accepts disabled thinking.
@pytest.mark.parametrize('thinking_level,model', [('default', 'claude-opus-5-5'),
                                                  ('off', 'claude-sonnet-4-6'),
                                                  ('high', 'claude-opus-5-5')])
async def test_native_reply_and_thinking_options(claude_http, thinking_level, model, tmp_path):
    replies, requests = claude_http
    replies.append([{'type': 'text', 'text': 'A complete '},
                    {'type': 'text', 'text': 'reply.'}])
    reply = await ChatClaude(model=model, thinking_level=thinking_level,
                             timeout=10).ainvoke([
        SystemMessage('Be brief.'), HumanMessage('@notes.md /help question')])
    assert reply.text == 'A complete reply.'
    assert not reply.tool_calls
    assert reply.response_metadata['session_id']
    assert reply.response_metadata['model_name'] == model
    [request] = requests
    content = request['messages'][0]['content']
    # Known models get CLI system reminders before the prompt; the prompt stays verbatim.
    assert (content if isinstance(content, str) else content[-1]['text']) == '@notes.md /help question'
    assert not request.get('tools')
    assert not list((tmp_path / 'claude-config' / 'projects').rglob('*.jsonl'))
    if thinking_level == 'high':
        assert request['thinking']['type'] == 'adaptive'
        assert request['output_config']['effort'] == 'high'
    elif thinking_level == 'off':
        assert request['thinking']['type'] == 'disabled'


async def test_system_prompt_over_the_argument_limit_reaches_claude(claude_http, tmp_path):
    # Linux refuses to spawn a process with one argument over 128 KiB. macOS has no such
    # per-argument limit, so the recorded command line is checked as well as the spawn.
    bundled = Path(claude_agent_sdk.__file__).parent / '_bundled' / 'claude'
    real_cli = str(bundled) if bundled.exists() else shutil.which('claude')
    recorded = tmp_path / 'argv'
    cli = tmp_path / 'claude-recording'
    cli.write_text(f'#!/bin/sh\nprintf \'%s\\0\' "$@" >> {shlex.quote(str(recorded))}\n'
                   f'exec {shlex.quote(real_cli)} "$@"\n')
    cli.chmod(0o755)
    project = tmp_path / 'project'
    project.mkdir()
    replies, requests = claude_http
    replies.append([{'type': 'text', 'text': 'Done.'}])
    first, second = 'Проверка ✓ ' * 10_000 + 'конец', 'Be brief.'
    assert len(first.encode()) > 200_000

    async with ChatClaude(cli_path=str(cli), cwd=project, timeout=30).async_session() as session:
        await session.ainvoke([SystemMessage(first), SystemMessage(second), HumanMessage('Hi')])

    [request] = requests
    assert f'{first}\n\n{second}' in [block['text'] for block in request['system']]
    argv = recorded.read_bytes().split(b'\0')
    assert max(map(len, argv)) < 128 * 1024
    prompt_file = Path(os.fsdecode(argv[argv.index(b'--system-prompt-file') + 1]))
    assert not prompt_file.exists()
    assert not prompt_file.is_relative_to(project)


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
