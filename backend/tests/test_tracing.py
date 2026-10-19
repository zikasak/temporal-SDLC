"""Exercise real LangChain/Langfuse integrations at the HTTP export boundary."""
import gzip
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


SECRET = 'private-provider-key-for-trace-tests'
EMAIL = 'private@example.com'


@pytest.fixture
def tracing_server():
    exported = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            if self.path == '/api/public/otel/v1/traces':
                if self.headers.get('Content-Encoding') == 'gzip':
                    body = gzip.decompress(body)
                request = ExportTraceServiceRequest.FromString(body)
                exported.extend(span for resource in request.resource_spans
                                for scope in resource.scope_spans for span in scope.spans)
                self.send_response(200)
                self.end_headers()
                return
            payload = json.loads(body)
            user = next(item['content'] for item in reversed(payload['messages'])
                        if item['role'] == 'user')
            if 'provider-error' in user:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': {'message': f'{SECRET} {EMAIL}',
                                                       'type': 'invalid_request_error'}}).encode())
                return
            if any(item['role'] == 'tool' for item in payload['messages']):
                message = {'role': 'assistant', 'content': f'Recorded {user}'}
            else:
                coding = any(item['function']['name'] == 'write_file'
                             for item in payload.get('tools', []))
                name = 'write_file' if coding else 'record_requirement'
                args = ({'path': 'done.txt', 'content': user} if coding else
                        {'category': 'functional', 'title': 'Search', 'description': user,
                         'priority': 'must'})
                message = {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'call_1', 'type': 'function',
                     'function': {'name': name, 'arguments': json.dumps(args)}}]}
            response = {'id': 'completion', 'object': 'chat.completion', 'created': 1,
                        'model': 'test-model',
                        'choices': [{'index': 0, 'message': message, 'finish_reason':
                                     'tool_calls' if 'tool_calls' in message else 'stop'}],
                        'usage': {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18}}
            if payload.get('stream'):
                # Mirror the provider: usage arrives in a trailing chunk only when requested.
                choice = response['choices'][0]
                chunk = {**response, 'object': 'chat.completion.chunk', 'usage': None}
                delta = message
                if 'tool_calls' in message:
                    delta = {**message, 'tool_calls': [{'index': 0, **message['tool_calls'][0]}]}
                chunks = [{**chunk, 'choices': [{'index': 0, 'delta': delta,
                                                 'finish_reason': None}]},
                          {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason':
                                                 choice['finish_reason']}]}]
                if (payload.get('stream_options') or {}).get('include_usage'):
                    chunks.append({**chunk, 'choices': [], 'usage': response['usage']})
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for item in chunks:
                    self.wfile.write(f'data: {json.dumps(item)}\n\n'.encode())
                self.wfile.write(b'data: [DONE]\n\n')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = {**os.environ, 'LANGFUSE_PUBLIC_KEY': 'pk-lf-local-tests',
                   'LANGFUSE_SECRET_KEY': 'sk-lf-local-tests',
                   'LANGFUSE_BASE_URL': f'http://127.0.0.1:{server.server_port}',
                   'LANGFUSE_TRACING_ENABLED': 'true', 'LANGFUSE_SAMPLE_RATE': '1',
                   'LANGFUSE_TRACING_ENVIRONMENT': 'test', 'LANGFUSE_RELEASE': 'test-release',
                   'OPENAI_API_KEY': SECRET,
                   'TEST_MODEL_SETTINGS': json.dumps({'adapter': 'openai', 'model': 'test-model'}),
                   'OPENAI_BASE_URL': f'http://127.0.0.1:{server.server_port}/v1',
                   'APP_TRACE_CONTENT_ENABLED': 'true'}
    # Do not inherit a developer's upstream context or exporter configuration.
    for key in list(environment):
        if key.startswith(('APP_TRACE_ID', 'APP_TRACE_PARENT', 'OTEL_')):
            del environment[key]
    try:
        yield environment, exported
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_script(script, environment):
    script = '''
import os
from app.llm import model
from app.settings.models import ModelSettings
async def saved_settings(workflow_type):
    return ModelSettings.model_validate_json(os.environ['TEST_MODEL_SETTINGS'])
model.get_workflow_settings = saved_settings
''' + script
    result = subprocess.run([sys.executable, '-c', script], env=environment,
                            cwd=Path(__file__).resolve().parents[1], text=True,
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result


def attributes(span):
    return {item.key: MessageToDict(item.value).get('stringValue',
                     MessageToDict(item.value)) for item in span.attributes}


def assert_no_sensitive_content(spans):
    wire = '\n'.join(str(span) for span in spans)
    assert SECRET not in wire
    assert EMAIL not in wire
    assert 'sk-lf-local-tests' not in wire


@pytest.mark.parametrize('capture_content', ['true', 'false'])
def test_concurrent_turns_export_isolated_traces_with_models_tools_and_masking(
        tracing_server, capture_content):
    environment, spans = tracing_server
    environment['APP_TRACE_CONTENT_ENABLED'] = capture_content
    run_script('''
import asyncio, os
from dataclasses import replace
from temporalio.testing import ActivityEnvironment
from app.temporal.activities import requirements_turn
from app.tracing import shutdown_tracing
async def turn(session):
    env = ActivityEnvironment()
    env.info = replace(env.info, workflow_id=session)
    text = f"Search {session} private@example.com {os.environ['OPENAI_API_KEY']}"
    result = await env.run(requirements_turn, [{'role':'user', 'content':text}], [])
    assert result['requirements'][0]['description'] == text
    assert result['reply'] == f'Recorded {text}'
async def main():
    await asyncio.gather(turn('session-a'), turn('session-b'))
asyncio.run(main())
shutdown_tracing()
''', environment)
    roots = [span for span in spans if span.name == 'gather-requirements']
    assert len(roots) == 2
    assert roots[0].trace_id != roots[1].trace_id
    by_id = {span.span_id: span for span in spans}
    for root in roots:
        related = [span for span in spans if span.trace_id == root.trace_id]
        session = attributes(root)['session.id']
        assert session in {'session-a', 'session-b'}
        for span in related:
            assert attributes(span)['session.id'] == session
            if span is not root:
                assert span.parent_span_id in by_id
                assert by_id[span.parent_span_id].trace_id == root.trace_id
        generations = [span for span in related
                       if attributes(span).get('langfuse.observation.type') == 'generation']
        assert len(generations) == 2
        assert all(span.parent_span_id == root.span_id for span in generations)
        for generation in generations:
            attrs = attributes(generation)
            assert attrs['langfuse.observation.model.name'] == 'test-model'
            usage = json.loads(attrs['langfuse.observation.usage_details'])
            assert usage['input'] == 11 and usage['output'] == 7
        tools = [span for span in related
                 if attributes(span).get('langfuse.observation.type') == 'tool']
        assert [span.name for span in tools] == ['record_requirement']
        assert tools[0].parent_span_id == root.span_id
        if capture_content == 'true':
            calls = [json.loads(attributes(span)['langfuse.observation.output']).get('tool_calls')
                     for span in generations]
            call, = next(items for items in calls if items)
            assert call['type'] == 'function'
            assert call['function']['name'] == 'record_requirement'
            assert json.loads(call['function']['arguments'])['title'] == 'Search'
        root_input = attributes(root)['langfuse.observation.input']
        if capture_content == 'true':
            assert session in root_input
            assert '[REDACTED EMAIL]' in root_input
            assert '[REDACTED]' in root_input
        else:
            assert 'Search session-' not in '\n'.join(str(span) for span in related)
            for span in related:
                for key in ('langfuse.observation.input', 'langfuse.observation.output'):
                    if key in attributes(span):
                        assert attributes(span)[key] == '[CONTENT CAPTURE DISABLED]'
    assert_no_sensitive_content(spans)


@pytest.mark.parametrize('tracing_mode', ['content', 'metadata', 'disabled', 'sampled-out'])
def test_claude_plugin_exports_native_generations_and_mcp_tools_under_activity(
        claude_http, tracing_server, tracing_mode, tmp_path):
    # The loopback CLI's environment is set first, so the turn's process inherits it.
    replies, _ = claude_http
    environment, spans = tracing_server
    environment['TEST_MODEL_SETTINGS'] = json.dumps({'adapter': 'claude', 'model': 'claude-test'})
    capture_content = 'true' if tracing_mode == 'content' else 'false'
    environment['APP_TRACE_CONTENT_ENABLED'] = capture_content
    environment['TEST_CLAUDE_TMP'] = str(tmp_path)
    if tracing_mode == 'disabled':
        environment['LANGFUSE_TRACING_ENABLED'] = 'false'
    if tracing_mode == 'sampled-out':
        environment['LANGFUSE_SAMPLE_RATE'] = '0'
    replies.extend([[{'type': 'thinking', 'thinking': 'Plan the synthetic search requirement',
                      'signature': 'native-signature'},
                     {'type': 'tool_use', 'id': 'call_1',
                      'name': 'mcp__flow__record_requirement', 'input': {
                          'category': 'functional', 'title': 'Search',
                          'description': f'Search listings {EMAIL} {SECRET}', 'priority': 'must'}}],
                    [{'type': 'text', 'text': 'Recorded'}]])
    run_script('''
import asyncio, os, tempfile
from pathlib import Path
from temporalio.testing import ActivityEnvironment
from app.temporal.activities import requirements_turn
from app.tracing import shutdown_tracing
tempfile.tempdir = os.environ['TEST_CLAUDE_TMP']
async def main():
    result = await ActivityEnvironment().run(requirements_turn,
        [{'role':'user', 'content':'Search listings'}], [])
    assert result['reply'] == 'Recorded'
    assert result['requirements'][0]['title'] == 'Search'
asyncio.run(main())
assert not list(Path(tempfile.tempdir).glob('claude-native-*'))
assert not list(Path(os.environ['CLAUDE_CONFIG_DIR']).rglob('*.jsonl'))
shutdown_tracing()
''', environment)
    if tracing_mode in {'disabled', 'sampled-out'}:
        assert not spans
        return
    root, = [span for span in spans if span.name == 'gather-requirements']
    native_turn, = [span for span in spans if span.name == 'Conversational Turn']
    assert native_turn.trace_id == root.trace_id
    assert native_turn.parent_span_id == root.span_id
    assert all(span.trace_id == root.trace_id for span in spans)
    assert all(attributes(span)['session.id'] == attributes(root)['session.id'] for span in spans)
    generations = [span for span in spans
                   if attributes(span).get('langfuse.observation.type') == 'generation']
    assert len(generations) == 2
    for generation in generations:
        assert generation.parent_span_id == native_turn.span_id
        attrs = attributes(generation)
        assert attrs['langfuse.observation.model.name'] == 'claude-test'
        usage = json.loads(attrs['langfuse.observation.usage_details'])
        # Cached prompt tokens are reported apart from the uncached input they add to.
        assert (usage['input'], usage['cache_read_input_tokens'], usage['cache_creation_input_tokens'],
                usage['output']) == (20, 7, 3, 5)
    tool, = [span for span in spans
             if attributes(span).get('langfuse.observation.type') == 'tool']
    assert tool.name == 'Tool: mcp__flow__record_requirement'
    assert tool.parent_span_id == native_turn.span_id
    wire = '\n'.join(str(span) for span in spans)
    if capture_content == 'true':
        assert 'Plan the synthetic search requirement' in wire
        assert 'Search listings' in wire
    else:
        assert 'Plan the synthetic search requirement' not in wire
        assert 'Search listings' not in wire
        for span in spans:
            for key in ('langfuse.observation.input', 'langfuse.observation.output'):
                if key in attributes(span):
                    assert attributes(span)[key] == '[CONTENT CAPTURE DISABLED]'
    assert_no_sensitive_content(spans)


@pytest.mark.parametrize('adapter', ['openai', 'claude'])
def test_agent_process_nests_under_host_stage_and_flushes_before_exit(
        claude_http, tracing_server, adapter):
    environment, spans = tracing_server
    if adapter == 'claude':
        environment['TEST_MODEL_SETTINGS'] = json.dumps({'adapter': 'claude', 'model': 'claude-test'})
        replies, _ = claude_http
        replies.extend([[{'type': 'tool_use', 'id': 'write', 'name': 'mcp__flow__write_file',
                          'input': {'path': 'done.txt', 'content': 'Search service'}}],
                        [{'type': 'text', 'text': 'Done'}]])
    run_script('''
import os, subprocess, sys, tempfile
from pathlib import Path
from app.settings.models import SETTINGS_FILENAME
from app.tracing import trace_operation, container_trace_environment, shutdown_tracing
child = """
from pathlib import Path
from app.agent_workflows.runtime import AgentProfile, run_agent
import asyncio
root=Path.cwd()
asyncio.run(run_agent(AgentProfile(root=root, prompt='Write done.txt, then finish',
    initial_messages=['Create a search service'], max_turns_env='TEST_AGENT_MAX_TURNS',
    validate_completion=lambda root: root.joinpath('done.txt').read_text(),
    completion_message='Done', exhaustion_message='Too many turns', default_max_turns=3)))
"""
with trace_operation('build-implementation', session_id='pipeline', feature='implementation') as span:
    env={**os.environ, **container_trace_environment('pipeline', 'implementation')}
    env['PYTHONPATH']=os.getcwd()
    with tempfile.TemporaryDirectory() as root:
        Path(root, SETTINGS_FILENAME).write_text(os.environ['TEST_MODEL_SETTINGS'])
        project = Path(root, 'service')
        project.mkdir()
        subprocess.run([sys.executable, '-c', child], cwd=project, env=env, check=True)
    span.update(output='Built')
shutdown_tracing()
''', environment)
    host, = [span for span in spans if span.name == 'build-implementation']
    agent, = [span for span in spans if span.name == 'author-implementation']
    assert attributes(agent)['langfuse.observation.type'] == 'agent'
    assert agent.parent_span_id == host.span_id
    assert all(span.trace_id == host.trace_id for span in spans)
    assert all(attributes(span)['session.id'] == 'pipeline' for span in spans)
    generations = [span for span in spans
                   if attributes(span).get('langfuse.observation.type') == 'generation']
    tool, = [span for span in spans
             if attributes(span).get('langfuse.observation.type') == 'tool']
    assert len(generations) == 2
    if adapter == 'claude':
        native_turn, = [span for span in spans if span.name == 'Conversational Turn']
        assert native_turn.parent_span_id == agent.span_id
        parent = native_turn
    else:
        parent = agent
    assert tool.parent_span_id == parent.span_id
    assert all(span.parent_span_id == parent.span_id for span in generations)
    # The MCP SDK's own spans of the same calls carry no content and are not exported.
    assert not [span for span in spans if span.name.startswith('tools/call')]
    assert_no_sensitive_content(spans)


def test_claude_agent_without_progress_times_out_and_still_exports_its_session(
        claude_http, tracing_server, tmp_path):
    replies, requests = claude_http
    environment, spans = tracing_server
    environment.update(TEST_MODEL_SETTINGS=json.dumps({'adapter': 'claude', 'model': 'claude-test'}),
                       TEST_WORKSPACE=str(tmp_path / 'stage'),
                       APP_TRACE_SESSION_ID='stalled-pipeline', APP_TRACE_AGENT_ROLE='implementation')
    # Together the replies outlast the timeout; only the last one is silent for longer.
    replies.extend([(1.5, [{'type': 'tool_use', 'id': 'first', 'name': 'mcp__flow__write_file',
                            'input': {'path': 'first.txt', 'content': 'first'}}]),
                    (1.5, [{'type': 'tool_use', 'id': 'second', 'name': 'mcp__flow__write_file',
                            'input': {'path': 'second.txt', 'content': 'second'}}]),
                    (6, [{'type': 'text', 'text': 'Too late'}])])
    run_script('''
import asyncio, os
from pathlib import Path
from app.agent_workflows import runtime
from app.settings.models import SETTINGS_FILENAME
from app.tracing import shutdown_tracing
create_adapter = runtime.create_adapter
def impatient_adapter(*args, **kwargs):
    provider = create_adapter(*args, **kwargs)
    provider.timeout = 2.5
    return provider
runtime.create_adapter = impatient_adapter
directory = Path(os.environ['TEST_WORKSPACE'])
root = directory / 'service'
root.mkdir(parents=True)
(directory / SETTINGS_FILENAME).write_text(os.environ['TEST_MODEL_SETTINGS'])
try:
    asyncio.run(runtime.run_agent(runtime.AgentProfile(root=root, prompt='Write files',
        initial_messages=['Create a service'], max_turns_env='TEST_AGENT_MAX_TURNS',
        validate_completion=lambda root: None, completion_message='Done',
        exhaustion_message='Too many turns')))
except TimeoutError as exc:
    assert 'sent nothing for 2.5 seconds' in str(exc)
else:
    raise AssertionError('The stalled agent finished')
assert (root / 'second.txt').read_text() == 'second'
shutdown_tracing()
''', environment)
    assert len(requests) == 3
    agent, = [span for span in spans if span.name == 'author-implementation']
    assert attributes(agent)['langfuse.observation.level'] == 'ERROR'
    native_turn, = [span for span in spans if span.name == 'Conversational Turn']
    assert native_turn.parent_span_id == agent.span_id
    tools = [span for span in spans if attributes(span).get('langfuse.observation.type') == 'tool']
    assert [span.name for span in tools] == ['Tool: mcp__flow__write_file'] * 2
    assert all(span.parent_span_id == native_turn.span_id for span in tools)
    generations = [span for span in spans
                   if attributes(span).get('langfuse.observation.type') == 'generation']
    assert len(generations) == 2


def test_claude_pause_resume_exports_each_native_call_once_under_its_current_stage(
        claude_http, tracing_server, tmp_path):
    replies, requests = claude_http
    environment, spans = tracing_server
    environment.update(TEST_MODEL_SETTINGS=json.dumps({'adapter': 'claude', 'model': 'claude-test'}),
                       TEST_WORKSPACE=str(tmp_path / 'paused'),
                       APP_TRACE_SESSION_ID='paused-pipeline', APP_TRACE_AGENT_ROLE='implementation')
    replies.append([{'type': 'tool_use', 'id': 'ask', 'name': 'mcp__flow__ask_user',
                     'input': {'questions': [{'question': 'Which currency?',
                                               'options': ['EUR', 'USD']}]}}])
    script = '''
import json, os
from pathlib import Path
from app.agent_workflows.runtime import AgentProfile, run_agent
import asyncio
from app.tracing import shutdown_tracing
from app.settings.models import SETTINGS_FILENAME
directory = Path(os.environ['TEST_WORKSPACE'])
directory.mkdir(exist_ok=True)
root = directory / 'service'
root.mkdir(exist_ok=True)
(directory / SETTINGS_FILENAME).write_text(os.environ['TEST_MODEL_SETTINGS'])
try:
    asyncio.run(run_agent(AgentProfile(root=root, prompt='Write done.txt, then finish',
        initial_messages=['Create a service'], max_turns_env='TEST_AGENT_MAX_TURNS',
        validate_completion=lambda root: root.joinpath('done.txt').read_text(),
        completion_message='Done', exhaustion_message='Too many turns')))
except SystemExit as exc:
    assert exc.code == 75
finally:
    shutdown_tracing()
'''
    run_script(script, environment)
    assert len(requests) == 1
    (tmp_path / 'paused/.agent-answers.json').write_text(json.dumps({'answers': ['EUR']}))
    replies.extend([[{'type': 'tool_use', 'id': 'write', 'name': 'mcp__flow__write_file',
                      'input': {'path': 'done.txt', 'content': 'EUR'}}],
                    [{'type': 'text', 'text': 'Done'}]])
    run_script(script, environment)
    assert len(requests) == 3
    stages = [span for span in spans if span.name == 'author-implementation']
    assert len(stages) == 2
    turns = [span for span in spans if span.name == 'Conversational Turn']
    assert len(turns) == 2
    assert {span.parent_span_id for span in turns} == {span.span_id for span in stages}
    generations = [span for span in spans
                   if attributes(span).get('langfuse.observation.type') == 'generation']
    assert len(generations) == 3
    assert len({(span.trace_id, span.span_id) for span in spans}) == len(spans)
    assert all(attributes(span)['session.id'] == 'paused-pipeline' for span in spans)


def test_provider_failure_still_raises_and_exports_a_redacted_error(tracing_server):
    environment, spans = tracing_server
    run_script('''
import asyncio
from openai import BadRequestError
from temporalio.testing import ActivityEnvironment
from app.temporal.activities import requirements_turn
from app.tracing import shutdown_tracing
async def main():
    try:
        await ActivityEnvironment().run(requirements_turn,
            [{'role':'user', 'content':'provider-error'}], [])
    except BadRequestError:
        pass
    else:
        raise AssertionError('Tracing swallowed the provider failure')
asyncio.run(main())
shutdown_tracing()
''', environment)
    root, = [span for span in spans if span.name == 'gather-requirements']
    assert attributes(root)['langfuse.observation.level'] == 'ERROR'
    assert 'RequestError' in attributes(root)['langfuse.observation.status_message']
    assert_no_sensitive_content(spans)


def test_disabled_tracing_does_not_export_or_change_results(tracing_server):
    environment, spans = tracing_server
    environment['LANGFUSE_TRACING_ENABLED'] = 'false'
    run_script('''
import asyncio
from temporalio.testing import ActivityEnvironment
from app.temporal.activities import requirements_turn
async def main():
    result = await ActivityEnvironment().run(requirements_turn,
        [{'role':'user', 'content':'Search listings'}], [])
    assert result['reply'] == 'Recorded Search listings'
    assert len(result['requirements']) == 1
asyncio.run(main())
''', environment)
    assert not spans
