"""Never send test payloads to a developer's configured Langfuse project."""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Set before application imports/.env loading. Tracing transport tests opt in
# explicitly in isolated child processes with disposable local credentials.
os.environ['LANGFUSE_TRACING_ENABLED'] = 'false'


@pytest.fixture
def claude_catalog(tmp_path):
    """A CLI that answers discovery requests and rejects any model prompt."""
    def install(server_info):
        script = tmp_path / 'claude'
        script.write_text(f'#!{sys.executable}\nimport json, sys\n'
                          f'SERVER_INFO = json.loads({json.dumps(server_info)!r})\n' + '''
if sys.argv[1:] == ['-v']:
    print('2.1.292 (Claude Code)')
    sys.exit()
for line in sys.stdin:
    item = json.loads(line)
    if item['type'] == 'user':
        raise RuntimeError('Discovery must never send a prompt')
    if item['type'] == 'control_request':
        print(json.dumps({'type': 'control_response', 'response': {
            'subtype': 'success', 'request_id': item['request_id'],
            'response': SERVER_INFO if item['request']['subtype'] == 'initialize' else {}}}),
            flush=True)
''')
        script.chmod(0o755)
        return str(script)

    return install


@pytest.fixture
def claude_http(monkeypatch, tmp_path):
    """The bundled Claude CLI against a loopback Anthropic endpoint: returns (replies,
    requests), where each reply is the content blocks of the next response, or an error
    body to answer with 401."""
    from app.settings.models import ModelSettings, SETTINGS_FILENAME

    requests, replies = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.split('?', 1)[0] != '/v1/messages':
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"input_tokens":100}')
                return
            requests.append(body)
            blocks = replies.pop(0) if replies else [{'type': 'text', 'text': 'Unexpected request'}]
            if isinstance(blocks, dict):
                replies.insert(0, blocks)
                self.send_response(401)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(blocks).encode())
                return
            called = any(block['type'] == 'tool_use' for block in blocks)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()

            def send(kind, **data):
                self.wfile.write(('event: ' + kind + '\ndata: '
                                 + json.dumps({'type': kind, **data}) + '\n\n').encode())
                self.wfile.flush()

            send('message_start', message={
                'id': f'msg_{len(requests)}', 'type': 'message', 'role': 'assistant',
                'model': body['model'], 'content': [], 'stop_reason': None,
                'stop_sequence': None, 'usage': {'input_tokens': 20, 'output_tokens': 0,
                    'cache_creation_input_tokens': 3, 'cache_read_input_tokens': 7}})
            for i, block in enumerate(blocks):
                if block['type'] == 'tool_use':
                    send('content_block_start', index=i, content_block={**block, 'input': {}})
                    send('content_block_delta', index=i, delta={
                        'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])})
                elif block['type'] == 'thinking':
                    send('content_block_start', index=i,
                         content_block={'type': 'thinking', 'thinking': ''})
                    send('content_block_delta', index=i, delta={
                        'type': 'thinking_delta', 'thinking': block['thinking']})
                    send('content_block_delta', index=i, delta={
                        'type': 'signature_delta', 'signature': block['signature']})
                else:
                    send('content_block_start', index=i, content_block={'type': 'text', 'text': ''})
                    send('content_block_delta', index=i,
                         delta={'type': 'text_delta', 'text': block['text']})
                send('content_block_stop', index=i)
                if block['type'] == 'tool_use' and i < len(blocks) - 1:
                    # The CLI runs a streamed call before the response ends.
                    time.sleep(0.5)
            send('message_delta', delta={
                'stop_reason': 'tool_use' if called else 'end_turn', 'stop_sequence': None},
                usage={'output_tokens': 5})
            send('message_stop')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_CUSTOM_HEADERS',
                 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('ANTHROPIC_BASE_URL', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'local-test-key')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-config'))
    monkeypatch.setenv('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', '1')
    monkeypatch.setenv('CLAUDE_CODE_MAX_RETRIES', '0')
    (tmp_path / SETTINGS_FILENAME).write_text(ModelSettings(
        adapter='claude', model='claude-opus-5-5', thinking_level='high').model_dump_json())
    try:
        yield replies, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture(autouse=True)
def saved_model_settings(monkeypatch):
    """Replace the external settings store for tests of the Docker lifecycle."""
    from app import docker_agents
    from app.settings.models import ModelSettings, WORKFLOW_LABELS

    settings = {key: ModelSettings(model='test-model') for key in WORKFLOW_LABELS}

    async def load(workflow_type):
        return settings[workflow_type]

    monkeypatch.setattr(docker_agents, 'get_workflow_settings', load)
    return settings


@pytest.fixture
def created_container(tmp_path, monkeypatch):
    """Run the lifecycle against stand-in Docker; return create args and settings input."""
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
            tmp_path / 'agent.log', paused=tmp_path / 'paused',
            image_build=docker_agents.default_agent_image('implementation', 'service'))
        from app.settings.models import ModelSettings, SETTINGS_FILENAME
        return created, ModelSettings.model_validate_json(
            (work / 'workspace' / SETTINGS_FILENAME).read_text())

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
