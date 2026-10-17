"""Settings catalog against the providers' HTTP and CLI protocols, without model calls."""
import json
import os
import sys

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes.settings import router
from app.settings import providers


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client


async def test_openai_catalog_uses_configured_endpoint_without_guessing_effort(client, monkeypatch):
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json={'data': [{'id': 'custom/model'}, {'id': 'reasoning-model'}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(serve), **kwargs))
    monkeypatch.setattr(providers, 'OPENAI_BASE_URL', 'https://configured-provider.test/v1/')
    monkeypatch.setattr(providers, 'OPENAI_API_KEY', 'test-key')
    monkeypatch.setattr(providers, 'PROVIDERS', {'openai': ('OpenAI', providers._openai_models)})
    response = await client.get('/settings/providers')
    assert response.status_code == 200
    models = response.json()['openai']['models']
    assert [(item['id'], item['thinking_levels']) for item in models] == [
        ('custom/model', ['default']), ('reasoning-model', ['default'])]
    assert len(requests) == 1
    assert str(requests[0].url) == 'https://configured-provider.test/v1/models'
    assert requests[0].headers['Authorization'] == 'Bearer test-key'


async def test_claude_catalog_reads_model_capabilities_without_sending_a_prompt(
        client, monkeypatch, fake_claude):
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    install, _ = fake_claude
    cli = install(server_info={'models': [
        {'value': 'smart', 'displayName': 'Smart', 'supportsEffort': True,
         'supportsAdaptiveThinking': True, 'supportedEffortLevels': ['low', 'high']},
        {'value': 'fast', 'displayName': 'Fast'},
    ]})
    monkeypatch.setattr(SubprocessCLITransport, '_find_cli', lambda _: cli)
    monkeypatch.setattr(providers, 'PROVIDERS', {'claude': ('Claude', providers._claude_models)})
    response = await client.get('/settings/providers')
    assert response.status_code == 200
    assert [(item['id'], item['thinking_levels']) for item in response.json()['claude']['models']] == [
        ('fast', ['default']), ('smart', ['default', 'low', 'high'])]


async def test_google_catalog_follows_pages_and_excludes_embedding_models(client, monkeypatch):
    requests = []

    def serve(request):
        requests.append(request)
        if request.url.params.get('pageToken') == 'next':
            return httpx.Response(200, json={'models': [
                {'name': 'models/custom-chat', 'displayName': 'Chat',
                 'supportedGenerationMethods': ['generateContent']}]})
        return httpx.Response(200, json={'models': [
            {'name': 'models/embedding', 'supportedGenerationMethods': ['embedContent']}],
            'nextPageToken': 'next'})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(serve), **kwargs))
    monkeypatch.setattr(providers, 'OPENAI_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/openai')
    monkeypatch.setattr(providers, 'OPENAI_API_KEY', 'google-test-key')
    monkeypatch.setattr(providers, 'PROVIDERS', {'openai': ('OpenAI', providers._openai_models)})
    response = await client.get('/settings/providers')
    assert response.status_code == 200
    assert response.json()['openai']['models'] == [
        {'id': 'custom-chat', 'label': 'Chat', 'thinking_levels': ['default']}]
    assert len(requests) == 2
    assert all(request.headers['x-goog-api-key'] == 'google-test-key' for request in requests)


async def test_pi_catalog_queries_levels_for_each_exact_model(client, tmp_path, monkeypatch):
    log = tmp_path / 'commands.jsonl'
    cli = tmp_path / 'pi'
    cli.write_text(f'#!{sys.executable}\n' + f'LOG = {str(log)!r}\n' + '''
import json, os, sys
selected = None
assert os.environ['PI_CODING_AGENT_DIR'].endswith('provider-config')
assert '--no-session' in sys.argv and '--no-extensions' in sys.argv
for line in sys.stdin:
    command = json.loads(line)
    with open(LOG, 'a') as log:
        log.write(json.dumps(command) + '\\n')
    kind = command['type']
    if kind == 'get_available_models':
        data = {'models': [{'provider': 'custom', 'id': 'fast', 'name': 'Fast'},
                           {'provider': 'custom', 'id': 'smart', 'name': 'Smart'}]}
    elif kind == 'set_model':
        assert command['provider'] == 'custom'
        selected = command['modelId']
        data = {}
    elif kind == 'get_available_thinking_levels':
        data = {'levels': ['off'] if selected == 'fast' else ['low', 'high', 'max']}
    else:
        raise AssertionError('Discovery must never send a prompt')
    print(json.dumps({'id': command['id'], 'type': 'response', 'success': True, 'data': data}), flush=True)
''')
    cli.chmod(0o755)
    monkeypatch.setenv('PATH', f'{tmp_path}{os.pathsep}{os.environ["PATH"]}')
    monkeypatch.setattr(providers, 'PI_CREDENTIALS_DIR', tmp_path / 'provider-config')
    monkeypatch.setattr(providers, 'PROVIDERS', {'pi': ('Pi', providers._pi_models)})
    response = await client.get('/settings/providers')
    assert response.status_code == 200
    assert [(item['id'], item['thinking_levels']) for item in response.json()['pi']['models']] == [
        ('custom/fast', ['default', 'off']), ('custom/smart', ['default', 'low', 'high', 'max'])]
    assert [json.loads(line)['type'] for line in log.read_text().splitlines()] == [
        'get_available_models', 'set_model', 'get_available_thinking_levels',
        'set_model', 'get_available_thinking_levels']
