"""Never send test payloads to a developer's configured Langfuse project."""
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Set before application imports/.env loading. Tracing transport tests opt in
# explicitly in isolated child processes with disposable local credentials.
os.environ['LANGFUSE_TRACING_ENABLED'] = 'false'
# A developer's harness would route scripted agents to the real Pi or Claude CLI; tests
# of those harnesses select them explicitly.
os.environ['LLM_HARNESS'] = ''

# Stands in for the Claude Code CLI behind the real Claude Agent SDK: answers the SDK's
# control requests, lists the tools the SDK serves, records what the turn was given and
# replies with the next scripted event stream.
FAKE_CLAUDE = '''
import json, os, pathlib, sys
if sys.argv[1:] == ['-v']:
    print('2.1.292 (Claude Code)')
    sys.exit()
argv = sys.argv[1:]
calls = json.loads(LOG.read_text()) if LOG.exists() else []

def send(item):
    print(json.dumps(item), flush=True)

def mcp(number, method, params=None):
    message = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
    if number:
        message['id'] = number
    send({'type': 'control_request', 'request_id': method,
          'request': {'subtype': 'mcp_message', 'server_name': 'langchain',
                      'message': message}})
    for line in sys.stdin:
        reply = json.loads(line)
        if reply['type'] == 'control_response' and reply['response']['request_id'] == method:
            return reply['response']['response']['mcp_response'].get('result')

for line in sys.stdin:
    item = json.loads(line)
    if item['type'] == 'control_request':
        send({'type': 'control_response', 'response': {
            'subtype': 'success', 'request_id': item['request_id'], 'response': {}}})
    if item['type'] != 'user':
        continue
    tools = []
    if '--mcp-config' in argv:
        mcp(1, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                              'clientInfo': {'name': 'fake', 'version': '1'}})
        mcp(None, 'notifications/initialized')
        tools = mcp(2, 'tools/list')['tools']
    session = next((arg.split('=', 1)[1] for arg in argv if arg.startswith('--resume=')), None)
    transcript = [json.loads(entry)['message'] for path in pathlib.Path(
        os.environ.get('CLAUDE_CONFIG_DIR', '')).glob(f'projects/*/{session}.jsonl')
        for entry in path.read_text().splitlines()] if session else []
    calls.append({'argv': argv, 'prompt': item, 'transcript': transcript, 'tools': tools})
    LOG.write_text(json.dumps(calls))
    events = RUNS[len(calls) - 1]
    for event in events:
        send(event)
    # The CLI exits non-zero after an error result.
    sys.exit(1 if events[-1].get('is_error') else 0)
'''


def claude_reply(*content, usage=None, **result):
    """One scripted run: an assistant event per block, as the CLI streams them, then the
    result. Strings are text and (id, name, input) tuples tool calls, which end the run on
    the turn limit as the real CLI does."""
    blocks = [{'type': 'thinking', 'thinking': '', 'signature': 'sig'}, *(
        {'type': 'text', 'text': item} if isinstance(item, str) else
        {'type': 'tool_use', 'id': item[0], 'name': item[1], 'input': item[2]}
        for item in content)]
    called = any(block['type'] == 'tool_use' for block in blocks)
    return [*({'type': 'assistant', 'session_id': 'session', 'parent_tool_use_id': None,
               'message': {'id': 'msg_1', 'role': 'assistant', 'model': 'claude-test',
                           'content': [block]}} for block in blocks),
            {'type': 'result', 'session_id': 'session', 'duration_ms': 1, 'duration_api_ms': 1,
             'num_turns': 2 if called else 1, 'total_cost_usd': 0.01,
             'subtype': 'error_max_turns' if called else 'success', 'is_error': called,
             'stop_reason': 'tool_use' if called else 'end_turn',
             'errors': ['Reached maximum number of turns (1)'] if called else [],
             'usage': usage or {'input_tokens': 20, 'output_tokens': 5,
                                'cache_read_input_tokens': 7,
                                'cache_creation_input_tokens': 3}, **result}]


@pytest.fixture
def fake_claude(tmp_path):
    """Returns (install, calls): install(*runs) writes a `claude` whose nth turn emits the
    nth list of events; calls() is what every turn received."""
    log = tmp_path / 'claude-calls.json'

    def install(*runs):
        script = tmp_path / 'claude'
        script.write_text(f'#!{sys.executable}\nimport json, pathlib\n'
                          f'LOG = pathlib.Path({str(log)!r})\n'
                          f'RUNS = json.loads({json.dumps(runs)!r})\n' + FAKE_CLAUDE)
        script.chmod(0o755)
        return str(script)

    return install, lambda: json.loads(log.read_text())


@pytest.fixture
def created_container(tmp_path, monkeypatch):
    """Runs the agent lifecycle against a stand-in Docker; returns `docker create` args."""
    async def run():
        created = []

        async def docker(*args, **kwargs):
            if args[0] == 'create':
                created.extend(args)
            return '{"ExitCode": 0}' if args[0] == 'inspect' else ''

        from app import docker_agents  # after the environment above is set

        monkeypatch.setattr(docker_agents, 'docker', docker)
        work = Path(tempfile.mkdtemp(dir=tmp_path))
        (work / 'workspace').mkdir()
        (work / 'workspace' / 'input.json').write_text('{}')
        await docker_agents.run_agent(
            'implementation', 'session-1', work / 'workspace', work / 'output', 'service',
            tmp_path / 'agent.log',
            image_build=docker_agents.default_agent_image('implementation', 'service'))
        return created

    return run


@pytest.fixture
def repository_selection():
    return {"provider": "github", "repository_id": 42, "full_name": "octocat/private-project",
            "html_url": "https://github.com/octocat/private-project", "private": True,
            "branch": "feature/one"}


@pytest.fixture(scope="session")
def app_private_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()).decode()


@pytest.fixture
def github_connection(monkeypatch, github_http, app_private_key):
    """save(token) stores GitHub App 123 (my-app), installed on octocat as installation
    99, whose installation tokens are `token`. Returns the stored row."""
    from datetime import datetime, timezone

    from app.github import storage

    def save(token):
        key = Fernet.generate_key()
        row = {"app_id": 123, "app_slug": "my-app", "installation_id": 99, "account": "octocat",
               "encrypted_private_key": Fernet(key).encrypt(app_private_key.encode()),
               "bot_user_id": 77, "validated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}

        async def get_connection():
            return row

        monkeypatch.setattr(storage, "GITHUB_TOKEN_ENCRYPTION_KEY", key.decode())
        monkeypatch.setattr(storage, "get_connection", get_connection)
        github_http[0]["/app/installations/99/access_tokens"] = httpx.Response(
            201, json={"token": token, "expires_at": "2026-01-01T01:00:00Z"})
        return row

    return save


@pytest.fixture
def github_http(monkeypatch):
    """Mock only GitHub's wire responses, not the connector's behavior."""
    from app.github import client

    responses = {}
    requests = []
    async_client = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        response = responses[request.url.path]
        if isinstance(response, Exception):
            raise response
        return response(request) if callable(response) else response

    def create_client(**kwargs):
        return async_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(client.httpx, "AsyncClient", create_client)
    return responses, requests
