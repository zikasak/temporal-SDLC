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

from tests.conftest import claude_reply

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
                   'OPENAI_API_KEY': SECRET, 'OPENAI_MODEL': 'test-model',
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


def test_claude_harness_turn_exports_generations_with_model_and_cached_usage(
        tracing_server, fake_claude):
    environment, spans = tracing_server
    install, _ = fake_claude
    environment.update(LLM_HARNESS='claude', CLAUDE_MODEL='claude-test', FAKE_CLAUDE=install(
        claude_reply(('call_1', 'mcp__langchain__record_requirement', {
            'category': 'functional', 'title': 'Search', 'description': 'Search listings',
            'priority': 'must'})),
        claude_reply('Recorded')))
    run_script('''
import asyncio, os
from functools import partial
from temporalio.testing import ActivityEnvironment
from app.llm import claude
claude.ChatClaude = partial(claude.ChatClaude, cli_path=os.environ['FAKE_CLAUDE'])
from app.temporal.activities import requirements_turn
from app.tracing import shutdown_tracing
async def main():
    result = await ActivityEnvironment().run(requirements_turn,
        [{'role':'user', 'content':'Search listings'}], [])
    assert result['reply'] == 'Recorded'
    assert result['requirements'][0]['title'] == 'Search'
asyncio.run(main())
shutdown_tracing()
''', environment)
    root, = [span for span in spans if span.name == 'gather-requirements']
    generations = [span for span in spans
                   if attributes(span).get('langfuse.observation.type') == 'generation']
    assert len(generations) == 2
    for generation in generations:
        assert generation.parent_span_id == root.span_id
        attrs = attributes(generation)
        assert attrs['langfuse.observation.model.name'] == 'claude-test'
        usage = json.loads(attrs['langfuse.observation.usage_details'])
        # Cached prompt tokens are reported apart from the uncached input they add to.
        assert (usage['input'], usage['input_cache_read'], usage['input_cache_creation'],
                usage['output']) == (20, 7, 3, 5)
    call, = next(calls for span in generations if (calls := json.loads(
        attributes(span)['langfuse.observation.output']).get('tool_calls')))
    assert call['function']['name'] == 'record_requirement'
    tool, = [span for span in spans
             if attributes(span).get('langfuse.observation.type') == 'tool']
    assert tool.name == 'record_requirement' and tool.parent_span_id == root.span_id
    assert_no_sensitive_content(spans)


def test_agent_process_nests_under_host_stage_and_flushes_before_exit(tracing_server):
    environment, spans = tracing_server
    run_script('''
import os, subprocess, sys, tempfile
from app.tracing import trace_operation, container_trace_environment, shutdown_tracing
child = """
from pathlib import Path
from app.agent_workflows.runtime import AgentProfile, run_agent
root=Path.cwd()
run_agent(AgentProfile(root=root, prompt='Write done.txt, then finish',
    initial_messages=['Create a search service'], max_turns_env='TEST_AGENT_MAX_TURNS',
    validate_completion=lambda root: root.joinpath('done.txt').read_text(),
    completion_message='Done', exhaustion_message='Too many turns', default_max_turns=3))
"""
with trace_operation('build-implementation', session_id='pipeline', feature='implementation') as span:
    env={**os.environ, **container_trace_environment('pipeline', 'implementation')}
    env['PYTHONPATH']=os.getcwd()
    with tempfile.TemporaryDirectory() as root:
        subprocess.run([sys.executable, '-c', child], cwd=root, env=env, check=True)
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
    tool, = [span for span in spans if span.name == 'write_file']
    assert len(generations) == 2
    assert tool.parent_span_id == agent.span_id
    assert all(span.parent_span_id == agent.span_id for span in generations)
    assert_no_sensitive_content(spans)


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
