"""Exercise the bundled Claude CLI against a loopback Anthropic endpoint."""
import copy
import json

import pytest

from app.agent_workflows import runtime
from app.implementation.agent import validate_completion
from app.llm.claude import ChatClaude


def native_call(identifier, name, **arguments):
    return {'type': 'tool_use', 'id': identifier, 'name': 'mcp__flow__' + name,
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


async def test_authoring_keeps_native_history_through_many_tool_turns(
    claude_http, tmp_path, monkeypatch
):
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

    await runtime.run_agent(runtime.AgentProfile(
        root=root, prompt='Implement the supplied contract', initial_messages=['requirements'],
        max_turns_env='TEST_AGENT_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Completed', exhaustion_message='Tool-turn limit exceeded',
    ))

    assert (root / 'Dockerfile').read_text() == 'FROM scratch'
    assert len(requests) == 14
    assert requests[0]['thinking']['type'] == 'adaptive'
    assert requests[0]['output_config']['effort'] == 'high'
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
    by_id = {block['tool_use_id']: ''.join(item['text'] for item in block['content'])
             if isinstance(block['content'], list) else block['content'] for block in results}
    assert by_id['write'] == 'Written'
    assert by_id['missing'].startswith('Tool failed: FileNotFoundError:')
    assert 'path' in by_id['invalid']
    assert [by_id[f'read_{i}'] for i in range(12)] == ['FROM scratch'] * 12
    assert 'No response requested.' not in str(messages)


@pytest.mark.parametrize('stop', ['limit', 'pause'])
async def test_authoring_stops_without_another_model_request(claude_http, tmp_path, monkeypatch, stop):
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
        await runtime.run_agent(profile)

    assert len(requests) == 1
    if stop == 'pause':
        (tmp_path / runtime.ANSWERS_FILENAME).write_text(json.dumps({'answers': ['EUR']}))
        replies.extend([[native_call('write', 'write_file', path='Dockerfile', content='FROM scratch')],
                        [{'type': 'text', 'text': 'Finished'}]])
        await runtime.run_agent(profile)
        assert (root / 'Dockerfile').read_text() == 'FROM scratch'
        assert 'EUR' in str(requests[1]['messages'])
        assert not (tmp_path / runtime.STATE_FILENAME).exists()


@pytest.mark.parametrize('turns', [1, 3, 5])
async def test_correction_queries_share_the_stage_turn_budget(
        claude_http, tmp_path, monkeypatch, turns):
    replies, requests = claude_http
    monkeypatch.setenv('TEST_AGENT_MAX_TURNS', str(turns))
    replies.extend([
        [{'type': 'text', 'text': 'Should I use the supplied contract?'}],
        [native_call('write', 'write_file', path='Dockerfile', content='FROM scratch')],
        [native_call('read', 'read_file', path='Dockerfile')],
        [{'type': 'text', 'text': 'Finished'}],
    ])
    root = tmp_path / 'service'
    root.mkdir()
    profile = runtime.AgentProfile(
        root=root, prompt='Implement the supplied contract', initial_messages=['requirements'],
        max_turns_env='TEST_AGENT_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Completed', exhaustion_message='Tool-turn limit exceeded')

    if turns < 5:
        with pytest.raises(RuntimeError, match='Tool-turn limit exceeded'):
            await runtime.run_agent(profile)
        assert len(requests) == turns
    else:
        await runtime.run_agent(profile)
        assert len(requests) == 4
        assert (root / 'Dockerfile').read_text() == 'FROM scratch'
        original = native_messages(requests[0])
        assert native_messages(requests[1])[:len(original)] == original
        assert runtime.DIRECT_QUESTION_NOTICE in str(requests[1]['messages'])


async def test_native_session_continues_followups_and_resumes_from_disk(claude_http, tmp_path):
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.tools import tool

    replies, requests = claude_http
    replies.extend([[native_call('first', 'lookup', term='first')],
                    [{'type': 'text', 'text': 'First reply'}],
                    [{'type': 'text', 'text': 'After follow-up'}],
                    [{'type': 'text', 'text': 'Resumed reply'}]])

    @tool
    def lookup(term: str) -> str:
        """Look up a term."""
        return term

    options = dict(model='claude-opus-5-5', timeout=10, cwd=tmp_path,
                   config_dir=tmp_path / 'native-config')
    provider = ChatClaude(**options)
    messages = [SystemMessage('Be brief'), HumanMessage('Original task')]
    async with provider.async_session():
        response = await provider.ainvoke(messages, tools=[lookup])
        response = await provider.ainvoke(
            [*messages, response, HumanMessage('Follow up')], tools=[lookup])
    resumed = ChatClaude(**options, resume=provider.session_id)
    assert (await resumed.ainvoke('Continue after restarting', tools=[lookup])).text == 'Resumed reply'

    assert resumed.session_id == provider.session_id
    assert len(requests) == 4
    for before, after in zip(requests, requests[1:]):
        previous = native_messages(before)
        assert native_messages(after)[:len(previous)] == previous
    assert 'Original task' in str(requests[-1]['messages'])
    assert 'First reply' in str(requests[-1]['messages'])
    assert 'Follow up' in str(requests[-1]['messages'])
    assert 'Continue after restarting' in str(requests[-1]['messages'])


async def test_native_session_propagates_provider_failure_and_closes(claude_http, tmp_path):
    replies, requests = claude_http
    replies.extend([[native_call('write', 'write_file', path='Dockerfile', content='FROM scratch')],
                    {'type': 'error', 'error': {'type': 'authentication_error',
                                              'message': 'Invalid local test credentials'}}])
    root = tmp_path / 'service'
    root.mkdir()
    with pytest.raises(RuntimeError, match='Invalid local test credentials'):
        await runtime.run_agent(runtime.AgentProfile(
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
