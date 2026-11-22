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
    assert '(external, temporal-sdlc,' in request['user_agent']
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
    assert real_cli is not None, 'the Claude CLI is neither bundled nor installed'
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


async def test_architecture_turns_keep_the_cached_system_prompt(claude_http, monkeypatch):
    # Each user turn is a new activity and a new native session; the provider reuses
    # the cached prompt only up to its first change, so the system prompt must not
    # carry the draft the previous turn recorded.
    from temporalio.testing import ActivityEnvironment

    from app.architecture import activities
    from app.architecture.models import ArchitectureState

    async def claude(workflow_type):
        return ChatClaude(model='claude-opus-5-5', timeout=30)

    monkeypatch.setattr(activities, 'get_model', claude)
    replies, requests = claude_http
    replies.extend([
        [{'type': 'tool_use', 'id': 'record', 'name': 'mcp__flow__record_component',
          'input': {'name': 'Gateway', 'responsibilities': ['Routes requests']}}],
        [{'type': 'text', 'text': 'Drafted the gateway.'}],
        [{'type': 'text', 'text': 'Noted.'}],
    ])
    state = ArchitectureState(requirements_document='FR-1: route requests')
    environment = ActivityEnvironment()

    first = await environment.run(activities.architecture_turn, state)
    state.structure = first['structure']
    state.messages = [{'role': 'assistant', 'content': first['reply']},
                      {'role': 'user', 'content': 'Looks good'}]
    await environment.run(activities.architecture_turn, state)

    def system(request):
        # The API strips the CLI's per-session billing header before caching.
        return [block for block in request['system']
                if not block['text'].startswith('x-anthropic-billing-header:')]

    opening, _, follow_up = requests
    assert system(follow_up) == system(opening)
    assert 'Gateway' not in str(opening['system'])
    assert 'Gateway' in str([item for item in follow_up['messages'] if item['role'] == 'user'])
