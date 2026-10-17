"""Settings API and provider selection against a disposable PostgreSQL database."""
import copy
import os

import psycopg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from app.api.routes.settings import router
from app.llm import model
from app.settings import storage


@pytest.fixture
async def database(monkeypatch):
    dsn = os.getenv('TEST_SETTINGS_DATABASE_URL')
    if not dsn:
        pytest.skip('Set TEST_SETTINGS_DATABASE_URL to a disposable settings_test_* database')
    if not conninfo_to_dict(dsn).get('dbname', '').startswith('settings_test_'):
        pytest.fail('Refusing settings tests: database name must start with settings_test_')

    async def connect():
        return await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row)

    monkeypatch.setattr(storage, 'connect', connect)
    await storage.ensure_schema()
    async with await connect() as conn:
        await conn.execute('TRUNCATE app.workflow_settings')
    await storage.ensure_schema()
    yield
    async with await connect() as conn:
        await conn.execute('TRUNCATE app.workflow_settings')


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
    workflows['chat'] = {'adapter': 'openai', 'model': '  saved-chat-model  ', 'thinking_level': 'low'}
    workflows['requirements'] = {'adapter': 'pi', 'model': 'openai/saved-pi-model', 'thinking_level': 'high'}
    workflows['api_design'] = {'adapter': 'claude', 'model': 'sonnet', 'thinking_level': 'max'}
    response = await client.put('/settings', json={'workflows': workflows})
    assert response.status_code == 200
    assert response.json()['workflows']['chat']['model'] == 'saved-chat-model'
    # Initialization after a process restart must retain all saved choices.
    await storage.ensure_schema()
    assert (await client.get('/settings')).json()['workflows'] == response.json()['workflows']

    chat = await model.get_model('chat')
    assert chat.model_name == 'saved-chat-model' and chat.reasoning_effort == 'low'
    requirements = await model.get_model('requirements')
    assert requirements.model == 'openai/saved-pi-model' and requirements.thinking_level == 'high'
    design = await model.get_model('api_design')
    assert design.model == 'sonnet' and design.thinking_level == 'max'

    workflows['chat'] = {'adapter': 'claude', 'model': 'opus', 'thinking_level': 'off'}
    assert (await client.put('/settings', json={'workflows': workflows})).status_code == 200
    updated = await model.get_model('chat')
    assert updated.model == 'opus' and updated.thinking_level == 'off'
    assert (await model.get_model('requirements')).model == 'openai/saved-pi-model'


async def test_invalid_settings_never_partially_overwrite_saved_choices(database, client):
    original = (await client.get('/settings')).json()['workflows']
    for patch in (
        {'adapter': 'unknown'}, {'thinking_level': 'off'}, {'model': '  '},
        {'model': 'bad\x00model'}, {'model': 'x' * 301}, {'unknown': True},
    ):
        workflows = copy.deepcopy(original)
        workflows['requirements']['model'] = 'must-not-be-saved'
        workflows['chat'].update(patch)
        assert (await client.put('/settings', json={'workflows': workflows})).status_code == 422
        assert (await client.get('/settings')).json()['workflows'] == original
    for workflows in ({}, {**original, 'unknown_workflow': original['chat']}):
        assert (await client.put('/settings', json={'workflows': workflows})).status_code == 422


async def test_unavailable_settings_store_returns_retryable_error(client, monkeypatch):
    async def unavailable():
        raise psycopg.OperationalError('database offline')
    monkeypatch.setattr(storage, 'connect', unavailable)
    response = await client.get('/settings')
    assert response.status_code == 503
    assert response.json()['detail'] == 'Request storage is unavailable'
