"""Drive the Pi adapter through a real subprocess standing in for the `pi` CLI."""
import asyncio
import json
import os
import sys

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from app import docker_agents
from app.agent_workflows import runtime
from app.implementation.agent import validate_completion
from app.llm import model as llm_model
from app.llm.pi import ChatPi

# Answers each invocation with the next scripted event stream and records what it was given.
FAKE_PI = '''#!{python}
import json, os, pathlib, sys
log = pathlib.Path({log!r})
calls = json.loads(log.read_text()) if log.exists() else []
calls.append({{'argv': sys.argv[1:], 'stdin': sys.stdin.read(),
              'agent_dir': os.environ.get('PI_CODING_AGENT_DIR'),
              'input': json.load(open(os.environ['PI_LANGCHAIN_INPUT']))}})
log.write_text(json.dumps(calls))
sys.stderr.write({stderr!r})
sys.stdout.write({runs!r}[len(calls) - 1])
sys.exit({code})
'''


def assistant(*content, stop='stop', **fields):
    blocks = [{'type': 'text', 'text': item} if isinstance(item, str) else
              {'type': 'toolCall', 'id': item[0], 'name': item[1], 'arguments': item[2]}
              for item in content]
    return {'type': 'message_end', 'message': {
        'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'hmm'}, *blocks],
        'provider': 'openai', 'model': 'gpt-test', 'stopReason': stop,
        'usage': {'input': 20, 'output': 5, 'cacheRead': 7, 'cacheWrite': 3,
                  'cost': {'input': 0.2, 'output': 0.25, 'cacheRead': 0.007,
                           'cacheWrite': 0.0375, 'total': 0.4945}}, **fields}}


@pytest.fixture
def fake_pi(tmp_path):
    """Returns (install, calls): install(*runs) writes a `pi` whose nth invocation emits
    the nth list of events; calls() is what every invocation received."""
    log = tmp_path / 'pi-calls.json'

    def install(*runs, code=0, stderr=''):
        streams = [''.join(json.dumps(event, ensure_ascii=False) + '\n' for event in run)
                   for run in runs]
        script = tmp_path / 'pi'
        script.write_text(FAKE_PI.format(python=sys.executable, log=str(log), runs=streams,
                                         stderr=stderr, code=code))
        script.chmod(0o755)
        return str(script)

    return install, lambda: json.loads(log.read_text())


def test_returns_final_text_and_usage(fake_pi):
    install, calls = fake_pi
    model = ChatPi(model='openai/gpt-test', command=install([
        {'type': 'session', 'version': 3},
        {'type': 'message_end', 'message': {'role': 'user', 'content': 'question'}},
        assistant('first attempt', stop='error', errorMessage='overloaded'),
        # A separator that is a line break to str.splitlines but not a record boundary.
        assistant('line one line two'),
        {'type': 'agent_settled'},
    ]))

    reply = model.invoke([SystemMessage('Be brief.'), HumanMessage('- question')])

    assert reply.content == 'line one line two'
    assert reply.tool_calls == []
    assert reply.usage_metadata == {'input_tokens': 30, 'output_tokens': 5, 'total_tokens': 35,
                                    'input_token_details': {'cache_read': 7, 'cache_creation': 3}}
    assert reply.response_metadata['model_name'] == 'gpt-test'
    [call] = calls()
    argv = call['argv']
    assert argv[argv.index('--system-prompt') + 1] == 'Be brief.'
    assert argv[argv.index('--model') + 1] == 'openai/gpt-test'
    assert {'--mode', 'json', '--no-tools', '--no-session'} <= set(argv)
    assert os.path.isfile(argv[argv.index('--extension') + 1])
    assert call['input']['tools'] == []
    assert [(item['role'], item['content']) for item in call['input']['messages']] == [
        ('user', [{'type': 'text', 'text': '- question'}])]


def test_history_is_sent_as_native_messages(fake_pi):
    install, calls = fake_pi
    ChatPi(command=install([assistant('ok')])).invoke([
        HumanMessage('Weather in Paris?'),
        AIMessage('Checking.', tool_calls=[
            {'name': 'get_weather', 'args': {'city': 'Paris'}, 'id': 'call-1'},
            {'name': 'get_time', 'args': {'city': 'Paris'}, 'id': 'call-2'}]),
        ToolMessage('17C', tool_call_id='call-1'),
        ToolMessage('unknown city', tool_call_id='call-2', status='error'),
    ])

    [call] = calls()
    assert '--system-prompt' not in call['argv'] and '--model' not in call['argv']
    user, asked, weather, clock = call['input']['messages']
    assert user['role'] == 'user'
    assert asked['role'] == 'assistant' and asked['stopReason'] == 'toolUse'
    assert asked['content'] == [
        {'type': 'text', 'text': 'Checking.'},
        {'type': 'toolCall', 'id': 'call-1', 'name': 'get_weather',
         'arguments': {'city': 'Paris'}},
        {'type': 'toolCall', 'id': 'call-2', 'name': 'get_time', 'arguments': {'city': 'Paris'}}]
    assert (weather['role'], weather['toolCallId'], weather['toolName'], weather['isError'],
            weather['content']) == ('toolResult', 'call-1', 'get_weather', False,
                                    [{'type': 'text', 'text': '17C'}])
    assert (clock['toolCallId'], clock['toolName'], clock['isError']) == (
        'call-2', 'get_time', True)
    # The history above replaces this prompt inside Pi; no conversation text goes here.
    assert 'Paris' not in call['stdin']


def test_requests_of_one_conversation_share_a_session_id(fake_pi):
    install, calls = fake_pi
    model = ChatPi(command=install([assistant('a')], [assistant('b')], [assistant('c')]))
    opening = [SystemMessage('Be brief.'), HumanMessage('Weather in Paris?')]

    model.invoke(opening)
    model.invoke([*opening, AIMessage('Sunny.'), HumanMessage('And tomorrow?')])
    model.invoke([SystemMessage('Be brief.'), HumanMessage('Weather in Rome?')])

    first, later, other = [call['argv'][call['argv'].index('--session-id') + 1]
                           for call in calls()]
    # Pi turns the session id into the provider's prompt cache key.
    assert first == later != other


def test_bound_tools_are_declared_and_their_calls_returned(fake_pi):
    install, calls = fake_pi

    @tool
    def lookup(term: str, limit: int = 5) -> str:
        """Look a term up."""
        return term

    model = ChatPi(command=install([
        assistant('Looking.', ('call-9', 'lookup', {'term': 'pi'}), stop='toolUse'),
        # What Pi emits when the extension stops a repeat request.
        assistant(stop='error', errorMessage='This operation was aborted'),
    ])).bind_tools([lookup], tool_choice='auto')

    reply = model.invoke('look up pi')

    assert reply.content == 'Looking.'
    assert reply.tool_calls == [
        {'name': 'lookup', 'args': {'term': 'pi'}, 'id': 'call-9', 'type': 'tool_call'}]
    [call] = calls()
    argv = call['argv']
    assert argv[argv.index('--tools') + 1] == 'lookup' and '--no-tools' not in argv
    [declared] = call['input']['tools']
    assert declared['name'] == 'lookup' and declared['description'] == 'Look a term up.'
    assert declared['parameters']['required'] == ['term']
    assert set(declared['parameters']['properties']) == {'term', 'limit'}


async def test_host_model_runs_pi_against_the_configured_directory(fake_pi, tmp_path, monkeypatch):
    install, calls = fake_pi
    monkeypatch.setenv('PI_CODING_AGENT_DIR', str(tmp_path / 'inherited'))
    from app.settings.models import ModelSettings
    async def load(workflow_type):
        return ModelSettings(adapter='pi', model='openai/gpt-test', thinking_level='high')
    monkeypatch.setattr(llm_model, 'get_workflow_settings', load)
    monkeypatch.setattr(llm_model, 'PI_CREDENTIALS_DIR', tmp_path / 'configured')
    install([assistant('ok')], [assistant('ok')])
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    model = await llm_model.get_model('chat')
    await model.ainvoke('hi')
    # Without a directory of its own the adapter leaves Pi's environment alone.
    await ChatPi().ainvoke('hi')

    assert [call['agent_dir'] for call in calls()] == [
        str(tmp_path / 'configured'), str(tmp_path / 'inherited')]
    argv = calls()[0]['argv']
    assert argv[argv.index('--model') + 1] == 'openai/gpt-test'
    assert argv[argv.index('--thinking') + 1] == 'high'


@pytest.mark.parametrize('cancel', [False, True])
async def test_interrupted_generation_reaps_pi_process(tmp_path, cancel):
    pid_file = tmp_path / 'pid'
    script = tmp_path / 'pi'
    script.write_text(f'#!{sys.executable}\n'
                      'import os, pathlib, time\n'
                      f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n'
                      'time.sleep(60)\n')
    script.chmod(0o755)
    model = ChatPi(command=str(script), timeout=10 if cancel else 0.5)
    task = asyncio.create_task(model.ainvoke('hi'))
    pid = None
    try:
        async with asyncio.timeout(5):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        pid = int(pid_file.read_text())
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if pid is not None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


def test_failed_response_raises(fake_pi):
    install, _ = fake_pi
    model = ChatPi(command=install([assistant(stop='error', errorMessage='quota exceeded')]))
    with pytest.raises(RuntimeError, match='quota exceeded'):
        model.invoke('hi')


# What Pi emits when the extension stops its recovery request after a cut-off response.
CUT_OFF = [assistant('I will', stop='length'),
           assistant(stop='aborted', errorMessage='This operation was aborted')]


def test_a_response_cut_off_at_the_output_limit_is_requested_again(fake_pi):
    install, calls = fake_pi
    model = ChatPi(command=install(CUT_OFF, [assistant('Done.')]))
    assert model.invoke('hi').content == 'Done.'
    assert len(calls()) == 2


def test_a_response_cut_off_every_time_fails_saying_so(fake_pi):
    install, calls = fake_pi
    model = ChatPi(command=install(CUT_OFF, CUT_OFF, CUT_OFF, [assistant('Done.')]))
    with pytest.raises(RuntimeError, match='cut off at the output limit 3 times'):
        model.invoke('hi')
    assert len(calls()) == 3


def test_process_failure_reports_stderr(fake_pi):
    install, _ = fake_pi
    with pytest.raises(RuntimeError, match='code 3: No API key'):
        ChatPi(command=install([], code=3, stderr='No API key\n')).invoke('hi')


def test_missing_response_raises(fake_pi):
    install, _ = fake_pi
    with pytest.raises(RuntimeError, match='no assistant response'):
        ChatPi(command=install([{'type': 'agent_settled'}])).invoke('hi')


async def test_coding_agent_runs_its_tools_through_pi(fake_pi, tmp_path, monkeypatch):
    install, calls = fake_pi
    install([assistant(('call-1', 'write_file',
                        {'path': 'Dockerfile', 'content': 'FROM scratch'}), stop='toolUse')],
            [assistant('Finished')])
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    from app.settings.models import ModelSettings, SETTINGS_FILENAME
    (tmp_path / SETTINGS_FILENAME).write_text(ModelSettings(
        adapter='pi', model='openai/gpt-test', thinking_level='high').model_dump_json())
    for name in ('OPENAI_MODEL', 'OPENAI_API_KEY', 'OPENAI_BASE_URL'):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / 'service'
    root.mkdir()

    await runtime.run_agent(runtime.AgentProfile(
        root=root, prompt='Implement the supplied contract', initial_messages=['requirements'],
        max_turns_env='TEST_AGENT_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Completed', exhaustion_message='Tool-turn limit exceeded',
    ))

    assert (root / 'Dockerfile').read_text() == 'FROM scratch'
    first, second = calls()
    assert first['argv'][first['argv'].index('--model') + 1] == 'openai/gpt-test'
    assert first['argv'][first['argv'].index('--thinking') + 1] == 'high'
    assert 'write_file' in first['argv'][first['argv'].index('--tools') + 1].split(',')
    assert [item['role'] for item in second['input']['messages']] == [
        'user', 'assistant', 'toolResult']
    assert second['input']['messages'][-1]['content'] == [{'type': 'text', 'text': 'Written'}]


async def test_pi_harness_mounts_credential_files_in_a_shared_directory(
        created_container, tmp_path, monkeypatch, saved_model_settings):
    credentials = tmp_path / 'pi'
    (credentials / 'extensions').mkdir(parents=True)
    for name in ('auth.json', 'models.json'):
        (credentials / name).write_text('{}')
    (credentials / 'settings.json').write_text(json.dumps({
        'packages': ['npm:some-extension'], 'defaultProvider': 'openai',
        'defaultModel': 'gpt-test', 'defaultThinkingLevel': 'high'}))
    from app.settings.models import ModelSettings
    saved_model_settings['implementation'] = ModelSettings(
        adapter='pi', model='openai/gpt-test', thinking_level='high')
    monkeypatch.setattr(docker_agents, 'PI_CREDENTIALS_DIR', credentials)

    args, settings = await created_container()

    mounts = [args[index + 1] for index, arg in enumerate(args) if arg == '-v']
    # Every container gets the same host directory, so Pi's auth.json.lock is shared.
    assert mounts == [f'{credentials}/containers:/home/agent/.pi/agent',
                      f'{credentials}/auth.json:/home/agent/.pi/agent/auth.json',
                      f'{credentials}/models.json:/home/agent/.pi/agent/models.json:ro']
    repeated, _ = await created_container()
    assert mounts == [repeated[index + 1] for index, arg in enumerate(repeated)
                      if arg == '-v']
    assert sorted(item.name for item in (credentials / 'containers').iterdir()) == [
        'auth.json', 'models.json']
    assert settings == saved_model_settings['implementation']


async def test_pi_oauth_agents_mount_the_shared_login_directory(
        created_container, tmp_path, monkeypatch, saved_model_settings):
    from app.settings.models import ModelSettings

    credentials = tmp_path / 'pi-credentials'
    credentials.mkdir()
    (credentials / 'auth.json').write_text('{}')
    monkeypatch.setattr(docker_agents, 'PI_CREDENTIALS_DIR', credentials)
    monkeypatch.setattr(docker_agents, 'PI_CREDENTIALS_SHARED', True, raising=False)
    saved_model_settings['implementation'] = ModelSettings(adapter='pi', model='openai-codex/test')

    args, _ = await created_container()
    mounts = [args[index + 1] for index, arg in enumerate(args) if arg == '-v']
    # The auth file and its sibling refresh lock must share the same parent on
    # the API, workers, and daemon; a second containers/ directory splits the lock.
    assert mounts == [f'{credentials}:/home/agent/.pi/agent']
    assert not (credentials / 'containers').exists()


async def test_pi_harness_requires_credentials(created_container, tmp_path, monkeypatch, saved_model_settings):
    from app.settings.models import ModelSettings
    saved_model_settings['implementation'] = ModelSettings(adapter='pi', model='openai/test')
    monkeypatch.setattr(docker_agents, 'PI_CREDENTIALS_DIR', tmp_path / 'absent')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with pytest.raises(RuntimeError, match='Pi credentials not found'):
        await created_container()


@pytest.mark.parametrize(('model', 'credential'), [
    ('openai/test', 'OPENAI_API_KEY'), ('anthropic/test', 'ANTHROPIC_API_KEY'),
])
@pytest.mark.parametrize('existing_auth_file', [False, True])
async def test_pi_harness_uses_api_keys_without_host_mounts(
        created_container, tmp_path, monkeypatch, saved_model_settings, model, credential,
        existing_auth_file):
    from app.settings.models import ModelSettings

    saved_model_settings['implementation'] = ModelSettings(adapter='pi', model=model)
    credentials = tmp_path / 'pi'
    if existing_auth_file:
        credentials.mkdir()
        (credentials / 'auth.json').write_text('{}')
    monkeypatch.setattr(docker_agents, 'PI_CREDENTIALS_DIR', credentials)
    monkeypatch.setenv(credential, 'private-pi-api-key')
    args, settings = await created_container()
    assert '-v' not in args
    assert credential in [args[index + 1] for index, arg in enumerate(args) if arg == '-e']
    assert 'private-pi-api-key' not in ' '.join(args)
    assert settings == saved_model_settings['implementation']
