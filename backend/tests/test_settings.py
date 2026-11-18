"""Settings API and provider selection against an in-memory stand-in of the settings
table: no database is used."""
import copy

import psycopg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langchain_openai import ChatOpenAI

from app.api.routes.settings import router
from app.llm import model
from app.llm.claude import ChatClaude
from app.llm.pi import ChatPi
from app.settings import storage
from app.settings import providers
from app.settings.models import ModelSettings, WORKFLOW_LABELS, WorkflowSettings


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
        async def load():
            return catalog[adapter]
        return load

    monkeypatch.setattr(providers, 'PROVIDERS', {
        adapter: (adapter.title(), discover(adapter)) for adapter in catalog})
    return catalog


@pytest.fixture
async def database(monkeypatch):
    """Replace the settings table with a dict holding each workflow's saved JSON."""
    table = {}

    async def ensure_schema():
        for workflow_type in WORKFLOW_LABELS:
            table.setdefault(workflow_type, ModelSettings().model_dump())

    async def get_settings():
        return WorkflowSettings(workflows=copy.deepcopy(table))

    async def get_workflow_settings(workflow_type):
        if workflow_type not in table:
            raise ValueError(f'Missing settings for workflow: {workflow_type}')
        return ModelSettings.model_validate(table[workflow_type])

    async def save_settings(settings):
        table.update({workflow_type: saved.model_dump()
                      for workflow_type, saved in settings.workflows.items()})
        return settings

    for function in (ensure_schema, get_settings, get_workflow_settings, save_settings):
        monkeypatch.setattr(storage, function.__name__, function)
    monkeypatch.setattr(model, 'get_workflow_settings', get_workflow_settings)
    await ensure_schema()


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        yield client


async def test_settings_persist_per_workflow_and_models_reload_after_save(database, client, monkeypatch):
    # Old selectors must have no effect on the saved settings.
    monkeypatch.setenv('LLM_HARNESS', 'claude')
    monkeypatch.setenv('OPENAI_MODEL', 'wrong-model')
    monkeypatch.setenv('PI_MODEL', 'wrong-pi-model')
    monkeypatch.setenv('CLAUDE_MODEL', 'wrong-claude-model')
    monkeypatch.setattr(model, 'OPENAI_API_KEY', 'local-test-key')
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
    assert isinstance(chat, ChatOpenAI)
    assert chat.model_name == 'saved-chat-model' and chat.reasoning_effort is None
    requirements = await model.get_model('requirements')
    assert isinstance(requirements, ChatPi)
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
    ):
        workflows = copy.deepcopy(original)
        workflows['requirements']['model'] = 'saved-chat-model'
        workflows['chat'].update(patch)
        assert (await client.put('/settings', json={'workflows': workflows})).status_code == 422
        assert (await client.get('/settings')).json()['workflows'] == original
    for workflows in ({}, {**original, 'unknown_workflow': original['chat']}):
        assert (await client.put('/settings', json={'workflows': workflows})).status_code == 422


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
    response = await client.get('/settings')
    assert response.status_code == 503
    assert response.json()['detail'] == 'Request storage is unavailable'
