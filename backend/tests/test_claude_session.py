"""Exercise the bundled Claude CLI against a loopback Anthropic endpoint."""
import copy
import json
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from app.agent_workflows import runtime
from app.implementation.agent import validate_completion
from app.llm import claude
from app.llm.claude import ChatClaude


@pytest.fixture
def claude_http(monkeypatch, tmp_path):
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
            send('message_delta', delta={
                'stop_reason': 'tool_use' if called else 'end_turn', 'stop_sequence': None},
                usage={'output_tokens': 5})
            send('message_stop')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_CUSTOM_HEADERS',
                 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('ANTHROPIC_BASE_URL', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'local-test-key')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-config'))
    monkeypatch.setenv('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', '1')
    monkeypatch.setenv('CLAUDE_CODE_MAX_RETRIES', '0')
    monkeypatch.setenv('LLM_HARNESS', 'claude')
    monkeypatch.setenv('CLAUDE_MODEL', 'claude-opus-5-5')
    monkeypatch.setattr(claude, 'ChatClaude', partial(ChatClaude, timeout=10))
    try:
        yield replies, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def native_call(identifier, name, **arguments):
    return {'type': 'tool_use', 'id': identifier, 'name': 'mcp__langchain__' + name,
            'input': arguments}


def native_messages(request):
    """Cache markers move forward; compare the prompt content they delimit."""
    messages = copy.deepcopy(request['messages'])
    for message in messages:
        content = message.get('content')
        if isinstance(content, list):
            for block in content:
                block.pop('cache_control', None)
            if len(content) == 1 and content[0]['type'] == 'text':
                message['content'] = content[0]['text']
    return messages


def test_authoring_keeps_native_history_through_many_tool_turns(claude_http, tmp_path, monkeypatch):
    from langchain_core.callbacks import BaseCallbackHandler

    usages = []

    class CaptureUsage(BaseCallbackHandler):
        def on_llm_end(self, response, **kwargs):
            usages.append(response.generations[0][0].message.usage_metadata)

    monkeypatch.setattr(runtime, 'langchain_config', lambda **kwargs: {
        'callbacks': [CaptureUsage()], **kwargs})
    replies, requests = claude_http
    thinking = {'type': 'thinking', 'thinking': 'Keep the complete native conversation.',
                'signature': 'native-signature'}
    replies.append([thinking, native_call('write', 'write_file', path='Dockerfile',
                                         content='FROM scratch'),
                    native_call('missing', 'read_file', path='missing.txt'),
                    native_call('invalid', 'read_file')])
    for i in range(12):
        replies.append([native_call(f'read_{i}', 'read_file', path='Dockerfile')])
    replies.append([{'type': 'text', 'text': 'Finished'}])
    root = tmp_path / 'service'
    root.mkdir()

    runtime.run_agent(runtime.AgentProfile(
        root=root, prompt='Implement the supplied contract', initial_messages=['requirements'],
        max_turns_env='TEST_AGENT_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Completed', exhaustion_message='Tool-turn limit exceeded',
    ))

    assert (root / 'Dockerfile').read_text() == 'FROM scratch'
    assert len(requests) == 14
    assert len(usages) == 14
    assert all(usage == {
        'input_tokens': 30, 'output_tokens': 5, 'total_tokens': 35,
        'input_token_details': {'cache_read': 7, 'cache_creation': 3}} for usage in usages)
    for before, after in zip(requests, requests[1:]):
        previous = native_messages(before)
        assert native_messages(after)[:len(previous)] == previous
        assert after['system'] == before['system']
        assert after['tools'] == before['tools']
    messages = native_messages(requests[-1])
    assert thinking in [block for message in messages if message['role'] == 'assistant'
                        for block in message['content']]
    results = [block for message in messages if message['role'] == 'user'
               and isinstance(message['content'], list) for block in message['content']
               if block['type'] == 'tool_result']
    by_id = {block['tool_use_id']: block['content'] for block in results}
    assert by_id['write'] == 'Written'
    assert by_id['missing'].startswith('Tool failed: FileNotFoundError:')
    assert 'path' in by_id['invalid']
    assert [by_id[f'read_{i}'] for i in range(12)] == ['FROM scratch'] * 12
    assert 'No response requested.' not in str(messages)
    assert claude.CONTINUE_PROMPT not in str(messages)


@pytest.mark.parametrize('stop', ['limit', 'pause'])
def test_authoring_stops_without_another_model_request(claude_http, tmp_path, monkeypatch, stop):
    replies, requests = claude_http
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
    profile = runtime.AgentProfile(
        root=root, prompt='Implement the supplied contract', initial_messages=['requirements'],
        max_turns_env='TEST_AGENT_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Completed', exhaustion_message='Tool-turn limit exceeded')

    with pytest.raises(exception, match=match):
        runtime.run_agent(profile)

    assert len(requests) == 1
    if stop == 'pause':
        (tmp_path / runtime.ANSWERS_FILENAME).write_text(json.dumps({'answers': ['EUR']}))
        replies.extend([[native_call('write', 'write_file', path='Dockerfile', content='FROM scratch')],
                        [{'type': 'text', 'text': 'Finished'}]])
        runtime.run_agent(profile)
        assert (root / 'Dockerfile').read_text() == 'FROM scratch'
        assert 'EUR' in str(requests[1]['messages'])
        assert not (tmp_path / runtime.STATE_FILENAME).exists()


def test_native_session_restarts_after_compaction_and_continues_followups(claude_http):
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.tools import tool

    replies, requests = claude_http
    replies.extend([[native_call('first', 'lookup', term='first')],
                    [{'type': 'text', 'text': 'After checkpoint'}],
                    [{'type': 'text', 'text': 'After follow-up'}]])

    @tool
    def lookup(term: str) -> str:
        """Look up a term."""
        return term

    provider = ChatClaude(model='claude-opus-5-5', timeout=10)
    model = provider.bind_tools([lookup])
    with provider.session():
        model.invoke([SystemMessage('Be brief'), HumanMessage('Original task')])
        checkpoint = [SystemMessage('Be brief'), HumanMessage('Checkpoint replaces old history')]
        response = model.invoke(checkpoint)
        model.invoke([*checkpoint, response, HumanMessage('Follow up')])

    assert len(requests) == 3
    assert 'Original task' not in str(requests[1]['messages'])
    previous = native_messages(requests[1])
    assert native_messages(requests[2])[:len(previous)] == previous
    assert 'After checkpoint' in str(requests[2]['messages'])
    assert 'Follow up' in str(requests[2]['messages'])


def test_native_session_propagates_provider_failure_and_closes(claude_http, tmp_path):
    replies, requests = claude_http
    replies.extend([[native_call('write', 'write_file', path='Dockerfile', content='FROM scratch')],
                    {'type': 'error', 'error': {'type': 'authentication_error',
                                              'message': 'Invalid local test credentials'}}])
    root = tmp_path / 'service'
    root.mkdir()
    with pytest.raises(RuntimeError, match='Invalid local test credentials'):
        runtime.run_agent(runtime.AgentProfile(
            root=root, prompt='Implement the contract', initial_messages=['requirements'],
            max_turns_env='TEST_AGENT_MAX_TURNS', validate_completion=validate_completion,
            completion_message='Completed', exhaustion_message='Tool-turn limit exceeded'))
    assert (root / 'Dockerfile').is_file()
    assert len(requests) >= 2
