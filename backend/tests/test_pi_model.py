"""Drive the Pi adapter: the real `pi` CLI, as the openai adapter runs it, against a
loopback OpenAI-compatible endpoint (the pi_http fixture). Pi runs the flow natively and
calls its tools over MCP, as Claude does (test_claude_session.py)."""
import asyncio
import json
import os
import shutil

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

from app import docker_agents
from app.agent_workflows import runtime
from app.implementation.agent import validate_completion
from app.llm.factory import create_adapter
from app.llm.pi import ChatPi
from app.settings.models import ModelSettings

needs_pi = pytest.mark.skipif(shutil.which('pi') is None, reason='the Pi CLI is not installed')


def native_call(identifier, name, **arguments):
    return {'type': 'tool_use', 'id': identifier, 'name': 'mcp__flow__' + name,
            'input': arguments}


def text(value):
    return [{'type': 'text', 'text': value}]


def endpoint_model(**options) -> ChatPi:
    """The openai adapter, calling the pi_http endpoint."""
    model = create_adapter(ModelSettings(adapter='openai', model='test-model'),
                           api_key=os.environ['OPENAI_API_KEY'],
                           base_url=os.environ['OPENAI_BASE_URL'])
    assert isinstance(model, ChatPi)
    for name, value in options.items():
        setattr(model, name, value)
    return model


def assert_appended(requests):
    """Every request continues the conversation of the one before it."""
    for before, after in zip(requests, requests[1:]):
        assert after['messages'][:len(before['messages'])] == before['messages']
        assert after.get('tools') == before.get('tools')


def content_text(message):
    content = message['content']
    return content if isinstance(content, str) else ''.join(
        part.get('text', '') for part in content)


def tool_results(request):
    return {message['tool_call_id']: content_text(message)
            for message in request['messages'] if message['role'] == 'tool'}


def profile(root, **options):
    return runtime.AgentProfile(
        root=root, prompt='Implement the supplied contract', initial_messages=['requirements'],
        max_turns_env='TEST_AGENT_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Completed', exhaustion_message='Tool-turn limit exceeded', **options)


@needs_pi
async def test_authoring_keeps_native_history_through_many_tool_turns(pi_http, tmp_path):
    replies, requests = pi_http
    replies.append([native_call('write', 'write_file', path='Dockerfile', content='FROM scratch'),
                    native_call('missing', 'read_file', path='missing.txt'),
                    native_call('invalid', 'read_file')])
    for i in range(3):
        replies.append([native_call(f'read_{i}', 'read_file', path='Dockerfile')])
    replies.append(text('Finished'))
    root = tmp_path / 'service'
    root.mkdir()

    await runtime.run_agent(profile(root))

    assert (root / 'Dockerfile').read_text() == 'FROM scratch'
    assert len(requests) == 5
    assert_appended(requests)
    # The stage's prompt as it is, and the endpoint's key; the flow's tools and codemode only.
    assert requests[0]['messages'][0] == {
        'role': 'system', 'content': 'Implement the supplied contract' + runtime.ASK_GUIDANCE}
    assert requests[0]['authorization'] == 'Bearer local-test-key'
    assert {item['function']['name'] for item in requests[0]['tools']} == {
        'mcp__flow__' + name for name in ('read_file', 'write_file', 'edit_file', 'list_files',
                                          'search_files', 'shell', 'ask_user')} | {'codemode'}
    results = tool_results(requests[-1])
    assert results['write'] == 'Written'
    assert results['missing'].startswith('Tool failed: FileNotFoundError:')
    assert 'path' in results['invalid']
    assert [results[f'read_{i}'] for i in range(3)] == ['FROM scratch'] * 3


@needs_pi
@pytest.mark.parametrize('stop', ['limit', 'pause'])
async def test_authoring_stops_without_another_model_request(pi_http, tmp_path, monkeypatch, stop):
    replies, requests = pi_http
    root = tmp_path / 'service'
    root.mkdir()
    if stop == 'limit':
        monkeypatch.setenv('TEST_AGENT_MAX_TURNS', '1')
        call = native_call('write', 'write_file', path='Dockerfile', content='FROM scratch')
        exception, match = RuntimeError, 'Tool-turn limit exceeded'
    else:
        call = native_call('ask', 'ask_user', questions=[{
            'question': 'Which currency?', 'options': ['EUR', 'USD']}])
        exception, match = SystemExit, '75'
    replies.append([call])

    with pytest.raises(exception, match=match):
        await runtime.run_agent(profile(root))

    assert len(requests) == 1
    if stop == 'pause':
        (tmp_path / runtime.ANSWERS_FILENAME).write_text(json.dumps({'answers': ['EUR']}))
        replies.extend([[native_call('write', 'write_file', path='Dockerfile',
                                     content='FROM scratch')], text('Finished')])
        await runtime.run_agent(profile(root))
        assert (root / 'Dockerfile').read_text() == 'FROM scratch'
        # The paused session goes on with the answers after its own question.
        assert requests[1]['messages'][:len(requests[0]['messages'])] == requests[0]['messages']
        assert 'EUR' in str(requests[1]['messages'][len(requests[0]['messages']):])
        assert not (tmp_path / runtime.STATE_FILENAME).exists()


@needs_pi
async def test_native_session_continues_followups_and_resumes_from_disk(pi_http, tmp_path):
    replies, requests = pi_http
    replies.extend([[native_call('first', 'lookup', term='first')], text('First reply'),
                    text('After follow-up'), text('Resumed reply')])

    @tool
    def lookup(term: str) -> str:
        """Look up a term."""
        return term.upper()

    options = dict(cwd=tmp_path, config_dir=tmp_path / 'native-config')
    provider = endpoint_model(**options)
    messages = [SystemMessage('Be brief'), HumanMessage('Original task')]
    async with provider.async_session():
        response = await provider.ainvoke(messages, tools=[lookup])
        assert response.text == 'First reply'
        response = await provider.ainvoke(
            [*messages, response, HumanMessage('Follow up')], tools=[lookup])
    resumed = endpoint_model(**options, resume=provider.session_id)
    assert (await resumed.ainvoke([SystemMessage('Be brief'),
                                   HumanMessage('Continue after restarting')],
                                  tools=[lookup])).text == 'Resumed reply'

    assert resumed.session_id == provider.session_id
    assert len(requests) == 4
    assert_appended(requests)
    conversation = str(requests[-1]['messages'])
    for part in ('Original task', 'FIRST', 'First reply', 'Follow up', 'After follow-up',
                 'Continue after restarting'):
        assert part in conversation


@needs_pi
def test_graph_turn_runs_tools_natively_and_shows_notices_before_the_next_request(pi_http):
    from app.llm.graph import get_message_graph, interview, native_session

    replies, requests = pi_http
    replies.extend([[native_call('first', 'lookup', term='first')],
                    [native_call('second', 'lookup', term='second')], text('Done')])
    notices = iter(['[System] Two minutes left.'])

    @tool
    def lookup(term: str) -> str:
        """Look up a term."""
        return term.upper()

    async def turn():
        provider = endpoint_model()
        graph = get_message_graph([lookup], model=provider,
                                  after_model_request=lambda: next(notices, None))
        async with native_session(provider):
            return await interview(
                graph, [SystemMessage('Be brief'), HumanMessage('Look both up')],
                {'recursion_limit': 10}, lambda: False)

    assert asyncio.run(turn()) == 'Done'
    assert len(requests) == 3
    assert_appended(requests)
    first, second = (tool_results(request) for request in requests[1:])
    assert (first['first'], second['second']) == ('FIRST', 'SECOND')
    # Shown once, after the results of the request that returned it.
    added = requests[1]['messages'][len(requests[0]['messages']):]
    assert '[System] Two minutes left.' in str(added[-1])
    assert str(requests[2]['messages']).count('Two minutes left') == 1


@needs_pi
def test_graph_turn_over_its_budget_stops_without_another_request(pi_http):
    from langgraph.errors import GraphRecursionError

    from app.llm.graph import get_message_graph

    replies, requests = pi_http
    replies.extend([[native_call(f'call_{i}', 'lookup', term='x')] for i in range(3)])

    @tool
    def lookup(term: str) -> str:
        """Look up a term."""
        return term

    graph = get_message_graph([lookup], model=endpoint_model())
    with pytest.raises(GraphRecursionError):
        asyncio.run(graph.ainvoke({'messages': [HumanMessage('Look it up')]},
                                  {'recursion_limit': 2}))
    assert len(requests) == 2


@needs_pi
def test_a_failure_deciding_after_a_turn_ends_the_run_with_it(pi_http):
    from app.llm.graph import get_message_graph

    replies, requests = pi_http
    replies.extend([[native_call('first', 'lookup', term='x')], text('Unchecked')])

    @tool
    def lookup(term: str) -> str:
        """Look up a term."""
        return term

    def heartbeat():
        raise RuntimeError('The activity was cancelled')

    graph = get_message_graph([lookup], model=endpoint_model(), after_model_request=heartbeat)
    with pytest.raises(RuntimeError, match='The activity was cancelled'):
        asyncio.run(graph.ainvoke({'messages': [HumanMessage('Look it up')]}))
    assert len(requests) == 1


@needs_pi
async def test_conversation_turns_resume_one_native_session_across_workers(
    pi_http, conversation_storage
):
    from app.llm.saved_conversations import ConversationRef

    replies, requests = pi_http
    replies += [text('First reply.'), text('Second reply.'), text('Third reply.')]
    system = SystemMessage('Interview the user.')

    async def turn(*history):
        # A new model per turn, as each turn is a new activity on any worker.
        provider = endpoint_model(conversation=ConversationRef('workflow-1/requirements'))
        return await provider.ainvoke([system, *history])

    await turn(HumanMessage('Kickoff\n\n<current_state>none</current_state>'))
    second = await turn(HumanMessage('Kickoff'), AIMessage('First reply.'),
                        HumanMessage('Add login\n\n<current_state>one</current_state>'))
    # An earlier message the session never saw: it cannot continue this conversation.
    await turn(HumanMessage('Another kickoff'), AIMessage('First reply.'),
               HumanMessage('Add login'), AIMessage('Second reply.'),
               HumanMessage('Add logout'))

    assert second.text == 'Second reply.'
    assert list(conversation_storage.objects) == ['workflow-1/requirements/openai.tar.gz']
    assert_appended(requests[:2])
    assert requests[1]['messages'][len(requests[0]['messages'])]['role'] == 'assistant'
    assert content_text(requests[1]['messages'][-1]) == (
        'Add login\n\n<current_state>one</current_state>')
    assert [message['role'] for message in requests[2]['messages']] == ['system', 'user']
    assert content_text(requests[2]['messages'][-1]).startswith('Conversation context:')


@needs_pi
async def test_native_session_propagates_provider_failure_and_closes(pi_http, tmp_path):
    replies, requests = pi_http
    replies.extend([[native_call('write', 'write_file', path='Dockerfile', content='FROM scratch')],
                    {'error': {'type': 'authentication_error',
                               'message': 'Invalid local test credentials'}}])
    root = tmp_path / 'service'
    root.mkdir()
    with pytest.raises(RuntimeError, match='Invalid local test credentials'):
        await runtime.run_agent(profile(root))
    assert (root / 'Dockerfile').is_file()
    assert len(requests) >= 2


@needs_pi
async def test_silent_model_times_out(pi_http):
    replies, _ = pi_http
    replies.append((5, text('Too late')))
    with pytest.raises(TimeoutError, match='Pi sent nothing for 1.5 seconds'):
        await endpoint_model(timeout=1.5).ainvoke('Hello')


async def test_session_server_answers_only_its_own_pi():
    """Every route of a session's loopback server takes the token only its Pi has."""
    from app.llm.pi import MCP_PATH, TRACE_PATH, TURN_PATH, _Server

    @tool
    def lookup(term: str) -> str:
        """Look up a term."""
        return term

    server = _Server('session-token', [lookup], lambda: False, lambda report: {'stop': True},
                     {'trace_id': '', 'span_id': '', 'session_id': '', 'feature': ''})
    initialize = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-06-18', 'capabilities': {},
        'clientInfo': {'name': 'test', 'version': '1'}}}
    mcp_headers = {'Accept': 'application/json, text/event-stream'}
    async with server.serve() as url, httpx.AsyncClient(base_url=url) as client:
        for token in ('', 'Bearer other-token'):
            headers = {'Authorization': token} if token else {}
            assert (await client.post(TURN_PATH, json={}, headers=headers)).status_code == 401
            assert (await client.post(MCP_PATH, json=initialize,
                                      headers={**mcp_headers, **headers})).status_code == 401
        assert (await client.post(TRACE_PATH + '/api/public/otel/v1/traces', content=b'{}',
                                  auth=('pk-lf-key', 'other-token'))).status_code == 401
        allowed = {'Authorization': 'Bearer session-token'}
        assert (await client.post(TURN_PATH, json={'toolResults': 1},
                                  headers=allowed)).json() == {'stop': True}
        assert (await client.post(MCP_PATH, json=initialize,
                                  headers={**mcp_headers, **allowed})).status_code == 200


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
