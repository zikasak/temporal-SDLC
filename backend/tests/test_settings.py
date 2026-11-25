"""Settings API and provider selection against an in-memory stand-in of the settings
and OpenAI endpoint tables: no database is used."""
import copy

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes import settings as settings_routes
from app.api.routes.auth import require_user
from app.api.routes.settings import router
from app.llm import model
from app.llm.claude import ChatClaude
from app.llm.pi import ChatPi
from app.settings import endpoint as endpoints
from app.settings import storage
from app.settings import providers
from app.settings.models import AgentSettings, ModelSettings, WORKFLOW_LABELS, WorkflowSettings

ADMIN_ID, OTHER_ID = 1, 2
SAVED_KEY = 'saved-endpoint-key'


@pytest.fixture(autouse=True)
def provider_catalog(monkeypatch):
    catalog = {
        'openai': [providers.ModelOption(id=name, label=name) for name in
                   (ModelSettings().model, 'saved-chat-model')],
        'pi': [providers.ModelOption(id='openai/saved-pi-model', label='Pi model',
                                    thinking_levels=['default', 'high'])],
        'claude': [providers.ModelOption(id=name, label=name, thinking_levels=levels)
                   for name, levels in [('sonnet', ['default', 'high', 'max']),
                                        ('opus', ['default', 'high']),
                                        ('haiku', ['default'])]],
    }

    def discover(adapter):
        async def load(*endpoint):
            return catalog[adapter]
        return load

    monkeypatch.setattr(providers, 'PROVIDERS', {
        adapter: (adapter.title(), discover(adapter)) for adapter in catalog})
    return catalog


@pytest.fixture
def endpoint_table(monkeypatch):
    """The OpenAI endpoint table: one saved endpoint, its key encrypted, to begin with."""
    monkeypatch.setattr(endpoints, 'SETTINGS_ENCRYPTION_KEY', Fernet.generate_key().decode())
    saved = {'endpoint': endpoints.StoredEndpoint(
        base_url='http://localhost:1234/v1', display_name=None,
        encrypted_api_key=endpoints.encrypt_api_key(SAVED_KEY))}

    async def get_stored_endpoint():
        return saved['endpoint']

    async def get_openai_endpoint():
        return endpoints.resolve(saved['endpoint'])

    monkeypatch.setattr(endpoints, 'get_stored_endpoint', get_stored_endpoint)
    monkeypatch.setattr(endpoints, 'get_openai_endpoint', get_openai_endpoint)
    monkeypatch.setattr(model, 'get_openai_endpoint', get_openai_endpoint)
    return saved


@pytest.fixture
async def database(monkeypatch, endpoint_table):
    """Replace the settings table with a dict holding each workflow's saved JSON."""
    table = {}

    async def ensure_schema():
        for workflow_type in WORKFLOW_LABELS:
            table.setdefault(workflow_type, ModelSettings().model_dump())

    async def get_settings():
        return WorkflowSettings(workflows=copy.deepcopy(
            {key: value for key, value in table.items() if key in WORKFLOW_LABELS}))

    async def get_workflow_settings(workflow_type):
        if workflow_type not in table:
            raise ValueError(f'Missing settings for workflow: {workflow_type}')
        return ModelSettings.model_validate(table[workflow_type])

    async def get_agent_settings():
        return AgentSettings.model_validate(table.get('agent', {}))

    async def save_settings(settings, endpoint=storage.UNCHANGED, encrypted_api_key=None,
                            agent=None):
        if agent is not None:
            table['agent'] = agent.model_dump()
        table.update({workflow_type: saved.model_dump()
                      for workflow_type, saved in settings.workflows.items()})
        if endpoint is not storage.UNCHANGED:
            endpoint_table['endpoint'] = None if endpoint is None else endpoints.StoredEndpoint(
                base_url=endpoint.base_url, display_name=endpoint.display_name,
                encrypted_api_key=encrypted_api_key)
        return settings

    for function in (ensure_schema, get_settings, get_workflow_settings, get_agent_settings,
                     save_settings):
        monkeypatch.setattr(storage, function.__name__, function)
    monkeypatch.setattr(model, 'get_workflow_settings', get_workflow_settings)
    await ensure_schema()
    return table


@pytest.fixture
def signed_in(monkeypatch):
    """The signed-in user: the administrator (the first user) unless changed."""
    user = {'id': ADMIN_ID, 'login': 'admin@example.com'}

    async def first_user_id():
        return ADMIN_ID

    monkeypatch.setattr(settings_routes.auth_storage, 'first_user_id', first_user_id)
    return user


@pytest.fixture
async def client(signed_in):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: signed_in
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        yield client


async def test_settings_persist_per_workflow_and_models_reload_after_save(database, client, monkeypatch):
    # Old selectors must have no effect on the saved settings.
    monkeypatch.setenv('LLM_HARNESS', 'claude')
    monkeypatch.setenv('OPENAI_MODEL', 'wrong-model')
    monkeypatch.setenv('PI_MODEL', 'wrong-pi-model')
    monkeypatch.setenv('CLAUDE_MODEL', 'wrong-claude-model')
    initial = await client.get('/settings')
    assert initial.status_code == 200
    workflows = initial.json()['workflows']
    workflows['chat'] = {'adapter': 'openai', 'model': '  saved-chat-model  ', 'thinking_level': 'default'}
    workflows['requirements'] = {'adapter': 'pi', 'model': 'openai/saved-pi-model', 'thinking_level': 'high'}
    workflows['api_design'] = {'adapter': 'claude', 'model': 'sonnet', 'thinking_level': 'max'}
    response = await client.put('/settings', json={'workflows': workflows})
    assert response.status_code == 200
    assert response.json()['workflows']['chat']['model'] == 'saved-chat-model'
    # Initialization after a process restart must retain all saved choices.
    await storage.ensure_schema()
    assert (await client.get('/settings')).json()['workflows'] == response.json()['workflows']

    chat = await model.get_model('chat')
    # The openai adapter is Pi calling the saved endpoint with its key.
    assert isinstance(chat, ChatPi)
    assert chat.model == 'saved-chat-model' and chat.thinking_level == 'default'
    assert chat.endpoint is not None and chat.endpoint.base_url == 'http://localhost:1234/v1'
    assert chat.endpoint.api_key.get_secret_value() == SAVED_KEY
    requirements = await model.get_model('requirements')
    assert isinstance(requirements, ChatPi) and requirements.endpoint is None
    assert requirements.model == 'openai/saved-pi-model' and requirements.thinking_level == 'high'
    design = await model.get_model('api_design')
    assert isinstance(design, ChatClaude)
    assert design.model == 'sonnet' and design.thinking_level == 'max'

    workflows['chat'] = {'adapter': 'claude', 'model': 'opus', 'thinking_level': 'high'}
    assert (await client.put('/settings', json={'workflows': workflows})).status_code == 200
    updated = await model.get_model('chat')
    assert isinstance(updated, ChatClaude)
    assert updated.model == 'opus' and updated.thinking_level == 'high'
    kept = await model.get_model('requirements')
    assert isinstance(kept, ChatPi) and kept.model == 'openai/saved-pi-model'


async def test_invalid_settings_never_partially_overwrite_saved_choices(database, client):
    original = (await client.get('/settings')).json()['workflows']
    for patch in (
        {'adapter': 'unknown'}, {'thinking_level': 'off'}, {'model': '  '},
        {'model': 'bad\x00model'}, {'model': 'x' * 301}, {'unknown': True},
        {'model': 'unknown-model'},
        {'adapter': 'claude', 'model': 'haiku', 'thinking_level': 'high'},
        {'adapter': 'claude', 'model': 'opus', 'thinking_level': 'max'},
        {'compaction_tokens': -1}, {'compaction_tokens': 3999},
    ):
        workflows = copy.deepcopy(original)
        workflows['requirements']['model'] = 'saved-chat-model'
        workflows['chat'].update(patch)
        assert (await client.put('/settings', json={'workflows': workflows})).status_code == 422
        assert (await client.get('/settings')).json()['workflows'] == original
    for workflows in ({}, {**original, 'unknown_workflow': original['chat']}):
        assert (await client.put('/settings', json={'workflows': workflows})).status_code == 422


async def test_compaction_starts_within_the_model_window_and_agent_settings_are_shared(
        database, client, provider_catalog):
    provider_catalog['pi'][0].context_window = 200000
    # Saved before the threshold existed: it compacts where the old default did.
    del database['review']['compaction_tokens']
    initial = (await client.get('/settings')).json()
    assert initial['workflows']['review']['compaction_tokens'] == 48000
    assert initial['providers']['pi']['models'][0]['context_window'] == 200000
    assert initial['workflows']['chat']['compaction_tokens'] == 48000
    assert initial['agent'] == {'contract_embed_chars': 32000}
    workflows = copy.deepcopy(initial['workflows'])
    workflows['chat'] = {'adapter': 'pi', 'model': 'openai/saved-pi-model',
                         'thinking_level': 'default', 'compaction_tokens': 200001}
    response = await client.put('/settings', json={'workflows': workflows,
                                                   'agent': {'contract_embed_chars': 60000}})
    assert response.status_code == 422
    assert 'context window of 200000 tokens' in response.json()['detail']
    assert (await client.get('/settings')).json()['agent'] == initial['agent']

    workflows['chat']['compaction_tokens'] = 150000
    # Claude compacts its own context, so its threshold is not held to a window.
    workflows['review'] = {'adapter': 'claude', 'model': 'opus', 'thinking_level': 'high',
                           'compaction_tokens': 10_000_000}
    response = await client.put('/settings', json={'workflows': workflows,
                                                   'agent': {'contract_embed_chars': 60000}})
    assert response.status_code == 200
    assert response.json()['agent'] == {'contract_embed_chars': 60000}
    assert (await storage.get_workflow_settings('chat')).compaction_tokens == 150000

    # Without agent settings in the body, the saved ones stay.
    workflows['chat']['compaction_tokens'] = 0
    response = await client.put('/settings', json={'workflows': workflows})
    assert response.status_code == 200
    saved = (await client.get('/settings')).json()
    assert saved['agent'] == {'contract_embed_chars': 60000}
    assert saved['workflows']['chat']['compaction_tokens'] == 0
    for agent in ({'contract_embed_chars': 999}, {'contract_embed_chars': 10_000_001},
                  {'unknown': 1}):
        assert (await client.put('/settings', json={'workflows': workflows,
                                                    'agent': agent})).status_code == 422
    assert (await client.get('/settings')).json()['agent'] == {'contract_embed_chars': 60000}


async def test_catalog_changes_and_provider_failure_never_overwrite_settings(
        database, client, provider_catalog, monkeypatch):
    initial = (await client.get('/settings')).json()
    assert initial['providers']['claude']['models'][0]['thinking_levels'] == ['default']
    original = initial['workflows']
    workflows = copy.deepcopy(original)
    workflows['chat'] = {'adapter': 'claude', 'model': 'sonnet', 'thinking_level': 'max'}
    # A model disappears after loading the page: validation uses fresh discovery.
    provider_catalog['claude'] = [item for item in provider_catalog['claude'] if item.id != 'sonnet']
    response = await client.put('/settings', json={'workflows': workflows})
    assert response.status_code == 422
    assert (await client.get('/settings')).json()['workflows'] == original

    async def offline():
        raise RuntimeError('private-token-must-not-leak')

    monkeypatch.setitem(providers.PROVIDERS, 'claude', ('Claude', offline))
    response = await client.get('/settings')
    assert response.status_code == 200
    assert response.json()['providers']['openai']['models']
    assert response.json()['providers']['claude']['error']
    assert 'private-token' not in response.text
    assert (await client.put('/settings', json={'workflows': workflows})).status_code == 503
    assert (await client.get('/settings')).json()['workflows'] == original


async def test_unavailable_settings_store_returns_retryable_error(client, monkeypatch):
    async def unavailable():
        raise psycopg.OperationalError('database offline')
    monkeypatch.setattr(storage, 'connect', unavailable)
    monkeypatch.setattr(endpoints, 'connect', unavailable)
    response = await client.get('/settings')
    assert response.status_code == 503
    assert response.json()['detail'] == 'Request storage is unavailable'


@pytest.fixture
def endpoint_server(monkeypatch):
    """Stands in for the OpenAI endpoint's GET /models; records what it was sent."""
    server = {'status': 200, 'requests': []}

    def serve(request):
        server['requests'].append(request)
        return httpx.Response(server['status'], json={'data': [{'id': 'remote-model'}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(endpoints.httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(serve), **kwargs))
    return server


def _all_pi(workflows):
    return {workflow: {'adapter': 'pi', 'model': 'openai/saved-pi-model', 'thinking_level': 'high'}
            for workflow in workflows}


async def test_endpoint_section_is_shaped_by_role_and_never_holds_the_key(
        database, client, signed_in):
    response = await client.get('/settings')
    assert response.status_code == 200
    assert response.json()['openai_endpoint'] == {
        'base_url': 'http://localhost:1234/v1', 'display_name': None, 'api_key_set': True}
    assert response.json()['providers']['openai']['label'] == 'Openai'
    assert SAVED_KEY not in response.text
    signed_in['id'] = OTHER_ID
    response = await client.get('/settings')
    assert response.json()['openai_endpoint'] == {'display_name': None}
    assert SAVED_KEY not in response.text


async def test_only_the_administrator_changes_the_endpoint(
        database, client, signed_in, endpoint_table, endpoint_server):
    signed_in['id'] = OTHER_ID
    original = endpoint_table['endpoint']
    workflows = (await client.get('/settings')).json()['workflows']
    for section in ({'base_url': 'https://other.test/v1', 'api_key': 'other-key'}, None):
        response = await client.put('/settings', json={'workflows': _all_pi(workflows),
                                                       'openai_endpoint': section})
        assert response.status_code == 403
    assert database['chat']['adapter'] == 'openai'
    # Without the section, their workflow settings are saved and the endpoint is kept.
    response = await client.put('/settings', json={'workflows': _all_pi(workflows)})
    assert response.status_code == 200
    assert response.json()['openai_endpoint'] == {'display_name': None}
    assert database['chat']['adapter'] == 'pi'
    assert endpoint_table['endpoint'] == original
    assert endpoint_server['requests'] == []


async def test_saving_the_endpoint_checks_it_first_and_a_failure_saves_nothing(
        database, client, endpoint_table, endpoint_server):
    original = endpoint_table['endpoint']
    workflows = (await client.get('/settings')).json()['workflows']
    changed = {**workflows, 'chat': {'adapter': 'pi', 'model': 'openai/saved-pi-model',
                                     'thinking_level': 'high'}}
    section = {'base_url': 'https://remote.test/v1/', 'api_key': 'new-secret-key',
               'display_name': '  Remote  '}
    endpoint_server['status'] = 401
    response = await client.put('/settings', json={'workflows': changed, 'openai_endpoint': section})
    assert response.status_code == 422
    assert '401' in response.json()['detail'] and 'new-secret-key' not in response.text
    assert endpoint_table['endpoint'] == original and database['chat']['adapter'] == 'openai'
    request = endpoint_server['requests'][-1]
    assert str(request.url) == 'https://remote.test/v1/models'
    assert request.headers['Authorization'] == 'Bearer new-secret-key'

    endpoint_server['status'] = 200
    response = await client.put('/settings', json={'workflows': changed, 'openai_endpoint': section})
    assert response.status_code == 200
    body = response.json()
    assert body['openai_endpoint'] == {'base_url': 'https://remote.test/v1/',
                                       'display_name': 'Remote', 'api_key_set': True}
    assert body['providers']['openai']['label'] == 'Remote'
    assert 'new-secret-key' not in response.text and database['chat']['adapter'] == 'pi'
    assert endpoints.resolve(endpoint_table['endpoint']).api_key == 'new-secret-key'

    # The display name alone changes without a check; the stored key is kept.
    sent = len(endpoint_server['requests'])
    response = await client.put('/settings', json={'workflows': changed, 'openai_endpoint': {
        'base_url': 'https://remote.test/v1/', 'display_name': ''}})
    assert response.status_code == 200
    assert response.json()['openai_endpoint']['display_name'] is None
    assert len(endpoint_server['requests']) == sent
    assert endpoints.resolve(endpoint_table['endpoint']).api_key == 'new-secret-key'
    # A new base URL needs a key: none is sent with an omitted one.
    response = await client.put('/settings', json={'workflows': changed, 'openai_endpoint': {
        'base_url': 'https://elsewhere.test/v1'}})
    assert response.status_code == 422
    assert len(endpoint_server['requests']) == sent
    # An empty key saves the endpoint without one.
    response = await client.put('/settings', json={'workflows': changed, 'openai_endpoint': {
        'base_url': 'https://elsewhere.test/v1', 'api_key': ''}})
    assert response.status_code == 200 and response.json()['openai_endpoint']['api_key_set'] is False
    assert 'Authorization' not in endpoint_server['requests'][-1].headers


async def test_invalid_endpoint_sections_answer_422_before_any_check(
        database, client, endpoint_table, endpoint_server):
    original = endpoint_table['endpoint']
    workflows = (await client.get('/settings')).json()['workflows']
    for section in ({'base_url': 'ftp://remote.test'}, {'base_url': 'not a url'},
                    {'base_url': 'https://remote.test/' + 'x' * 2048, 'api_key': 'k'},
                    {'base_url': 'https://remote.test', 'api_key': 'k', 'display_name': 'x' * 101},
                    {'api_key': 'secret-in-a-bad-body'}, {'base_url': 7, 'api_key': ['secret-list']}):
        response = await client.put('/settings', json={'workflows': workflows,
                                                       'openai_endpoint': section})
        assert response.status_code == 422
        assert 'secret' not in response.text
    assert endpoint_server['requests'] == [] and endpoint_table['endpoint'] == original


async def test_removing_the_endpoint_is_blocked_while_workflow_types_use_openai(
        database, client, endpoint_table):
    workflows = (await client.get('/settings')).json()['workflows']
    workflows = {**_all_pi(workflows), 'chat': workflows['chat'], 'review': workflows['review']}
    assert (await client.put('/settings', json={'workflows': workflows})).status_code == 200
    response = await client.put('/settings', json={'workflows': workflows, 'openai_endpoint': None})
    assert response.status_code == 409
    assert response.json()['workflow_types'] == ['chat', 'review']
    assert 'chat' in response.json()['detail'] and 'review' in response.json()['detail']
    assert endpoint_table['endpoint'] is not None

    workflows = _all_pi(workflows)
    assert (await client.put('/settings', json={'workflows': workflows})).status_code == 200
    response = await client.put('/settings', json={'workflows': workflows, 'openai_endpoint': None})
    assert response.status_code == 200
    assert response.json()['openai_endpoint'] == {'base_url': None, 'display_name': None,
                                                  'api_key_set': False}
    assert 'openai' not in response.json()['providers'] and endpoint_table['endpoint'] is None
    # Removing it again is a no-op; the openai adapter is no longer offered.
    response = await client.put('/settings', json={'workflows': workflows, 'openai_endpoint': None})
    assert response.status_code == 200
    workflows['chat'] = {'adapter': 'openai', 'model': 'saved-chat-model', 'thinking_level': 'default'}
    assert (await client.put('/settings', json={'workflows': workflows})).status_code == 422


async def test_a_saved_openai_choice_fails_without_an_endpoint(database, client, endpoint_table):
    endpoint_table['endpoint'] = None
    # The chat type's saved adapter is openai: it keeps that choice, and its runs fail.
    with pytest.raises(model.OpenAIEndpointMissing, match='no OpenAI endpoint is set up'):
        await model.get_model('chat')


def test_a_workflow_type_with_no_saved_adapter_defaults_to_pi_until_openai_is_set_up():
    from app.settings.models import default_model_settings

    assert default_model_settings(False).adapter == 'pi'
    assert default_model_settings(True).adapter == 'openai'
