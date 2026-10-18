"""Exercise the bundled Claude CLI against a loopback Anthropic endpoint."""
import copy
import json

import pytest

from app.agent_workflows import runtime
from app.implementation.agent import validate_completion
from app.llm import claude
from app.llm.claude import ChatClaude


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
    assert requests[0]['thinking']['type'] == 'adaptive'
    assert requests[0]['output_config']['effort'] == 'high'
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


def test_interview_graph_appends_to_one_native_conversation(claude_http):
    import asyncio

    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.tools import tool

    from app.llm.graph import get_message_graph, interview, native_session

    replies, requests = claude_http
    replies.extend([[native_call('first', 'lookup', term='first')],
                    [native_call('second', 'lookup', term='second')],
                    [{'type': 'text', 'text': 'Done'}]])

    @tool
    def lookup(term: str) -> str:
        """Look up a term."""
        return term.upper()

    async def turn():
        provider = ChatClaude(model='claude-opus-5-5', timeout=10)
        graph = get_message_graph([lookup], model=provider)
        async with native_session(provider):
            return await interview(
                graph, [SystemMessage('Be brief'), HumanMessage('Look both up')],
                {'recursion_limit': 10}, lambda: False)

    assert asyncio.run(turn()) == 'Done'
    assert len(requests) == 3
    for before, after in zip(requests, requests[1:]):
        previous = native_messages(before)
        assert native_messages(after)[:len(previous)] == previous
    messages = str(native_messages(requests[-1]))
    assert 'FIRST' in messages and 'SECOND' in messages
    assert 'No response requested.' not in messages
    assert claude.CONTINUE_PROMPT not in messages
