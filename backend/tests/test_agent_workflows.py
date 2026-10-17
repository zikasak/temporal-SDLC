import json
import shlex
import sys
import tarfile
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.agent_workflows import contracts, runtime
from app.agent_workflows.host import StageInputs, StageSpec, copy_regular_files, run_stage
from app.api_design.openapi import render_openapi
from app.implementation import activities as implementation
from app.implementation.agent import validate_completion as validate_implementation
from app.requirements.document import render_requirements_markdown
from app.languages import get_language
from app.test_generation import activities as test_generation
from app.test_generation.agent import validate_completion as validate_tests


@pytest.fixture
def scripted_model(monkeypatch):
    """Replace only the external provider; tools and completion checks remain real."""
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://provider.example/v1')
    monkeypatch.delenv('TEST_AGENT_MAX_TURNS', raising=False)
    monkeypatch.delenv('AGENT_CONTEXT_TOKENS', raising=False)

    def install(responses):
        class Model:
            def __init__(self):
                self.responses = iter(responses)
                self.messages = []
                self.requests = []

            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                self.messages = list(messages)
                self.requests.append(self.messages)
                return next(self.responses)

        model = Model()
        from app.llm import factory
        monkeypatch.setattr(factory, 'create_model', lambda **settings: model)
        return model

    return install


@pytest.fixture
def profile(tmp_path):
    from app.settings.models import ModelSettings, SETTINGS_FILENAME
    (tmp_path / SETTINGS_FILENAME).write_text(ModelSettings(model='test-model').model_dump_json())
    root = tmp_path / 'service'
    root.mkdir()
    return runtime.AgentProfile(
        root=root, prompt='Implement the supplied contract', initial_messages=['requirements'],
        max_turns_env='TEST_AGENT_MAX_TURNS', validate_completion=validate_implementation,
        completion_message='Completed', exhaustion_message='Tool-turn limit exceeded',
    )


ASK = {'name': 'ask_user', 'id': 'ask', 'args': {'questions': [
    {'question': 'Which currency?', 'options': ['EUR', 'USD']}]}}


def test_agent_pauses_for_answers_and_resumes_where_it_stopped(tmp_path, profile, scripted_model):
    # Calls after ask_user still run before the pause.
    scripted_model([AIMessage(content='', tool_calls=[
        ASK, {'name': 'write_file', 'args': {'path': 'Orders.java', 'content': 'class Orders {}'},
              'id': 'orders'}])])
    with pytest.raises(SystemExit) as paused:
        runtime.run_agent(profile)
    assert paused.value.code == runtime.PAUSE_EXIT
    state = json.loads((tmp_path / runtime.STATE_FILENAME).read_text())
    assert state['pending']['questions'] == ASK['args']['questions']

    # The host hands the paused workspace and the answers to a new container.
    (tmp_path / runtime.ANSWERS_FILENAME).write_text(json.dumps({'answers': ['EUR']}))
    model = scripted_model([
        AIMessage(content='', tool_calls=[
            {'name': 'write_file', 'args': {'path': 'Dockerfile', 'content': 'FROM scratch'},
             'id': 'dockerfile'}]),
        AIMessage(content='Finished'),
    ])
    runtime.run_agent(profile)
    system, inputs, call, answered, written = model.requests[0]
    assert system.content == profile.prompt + runtime.ASK_GUIDANCE
    # The results follow the order of the calls.
    assert [item['id'] for item in call.tool_calls] == ['ask', 'orders']
    assert written.tool_call_id == 'orders'
    assert answered.tool_call_id == 'ask' and json.loads(answered.content) == {
        'answers': [{'question': 'Which currency?', 'answer': 'EUR'}]}
    assert not (tmp_path / runtime.STATE_FILENAME).exists()
    assert (profile.root / 'Orders.java').is_file()


def test_agent_is_sent_back_when_it_asks_in_its_text(profile, scripted_model):
    model = scripted_model([
        AIMessage(content='Should the service store prices in cents?'),
        AIMessage(content='', tool_calls=[
            {'name': 'write_file', 'args': {'path': 'Dockerfile', 'content': 'FROM scratch'},
             'id': 'written'}]),
        AIMessage(content='Done: prices are stored in cents.'),
    ])
    runtime.run_agent(profile)
    assert model.requests[1][-1].content == runtime.DIRECT_QUESTION_NOTICE
    assert len(model.requests) == 3


def test_agent_executes_tools_and_returns_correlated_errors(profile, scripted_model):
    model = scripted_model([
        AIMessage(content='', tool_calls=[
            {'name': 'read_file', 'args': {'path': 'missing.txt'}, 'id': 'missing'},
            {'name': 'write_file', 'args': {'path': 'Dockerfile', 'content': 'FROM scratch'},
             'id': 'written'},
        ]),
        AIMessage(content='Finished'),
    ])
    runtime.run_agent(profile)
    assert (profile.root / 'Dockerfile').read_text() == 'FROM scratch'
    results = {message.tool_call_id: message.content for message in model.messages
               if isinstance(message, ToolMessage)}
    assert results['written'] == 'Written'
    assert results['missing'].startswith('Tool failed: FileNotFoundError:')
    assert 'missing.txt' in results['missing']


@pytest.mark.parametrize('validator,error', [
    (validate_implementation, 'without a Dockerfile'),
    (partial(validate_tests, get_language('java').test_suite.test_glob), 'without test files'),
])
def test_agent_cannot_finish_without_role_artifacts(profile, scripted_model, validator, error):
    scripted_model([AIMessage(content='Finished')])
    with pytest.raises(RuntimeError, match=error):
        runtime.run_agent(replace(profile, validate_completion=validator))


def test_agent_stops_at_configured_turn_limit(profile, scripted_model, monkeypatch):
    monkeypatch.setenv('TEST_AGENT_MAX_TURNS', '2')
    scripted_model([
        AIMessage(content='', tool_calls=[
            {'name': 'write_file', 'args': {'path': 'progress.txt', 'content': content}, 'id': content},
        ]) for content in ['first', 'second', 'third']
    ])
    with pytest.raises(RuntimeError, match='Tool-turn limit exceeded'):
        runtime.run_agent(profile)
    assert (profile.root / 'progress.txt').read_text() == 'second'


def write(path, content='x'):
    return AIMessage(content='', tool_calls=[
        {'name': 'write_file', 'args': {'path': path, 'content': content}, 'id': path}])


def assert_calls_and_results_correlate(messages):
    """Every provider rejects a tool result without its call, or a call without results."""
    pending = []
    for message in messages:
        if isinstance(message, ToolMessage):
            assert message.tool_call_id == pending.pop(0)
            continue
        assert not pending
        pending = [call['id'] for call in getattr(message, 'tool_calls', [])]
    assert not pending


def test_stale_history_becomes_a_checkpoint_within_the_context_budget(
        profile, scripted_model, monkeypatch, capsys):
    monkeypatch.setenv('AGENT_CONTEXT_TOKENS', '6000')
    source = 'class Body {}\n' * 430
    model = scripted_model([
        write('Orders.java', source),
        write('Stock.java', source),
        AIMessage(content='Decisions: money is stored in cents\nRemaining: Payments, Dockerfile'),
        write('Payments.java', source),
        AIMessage(content='Decisions: money is stored in cents\nRemaining: Dockerfile'),
        AIMessage(content='', tool_calls=[
            {'name': 'read_file', 'args': {'path': 'missing.txt'}, 'id': 'missing'},
            {'name': 'write_file', 'args': {'path': 'Dockerfile', 'content': 'FROM scratch'},
             'id': 'docker'},
        ]),
        AIMessage(content='Finished'),
    ])
    runtime.run_agent(profile)

    first_turn, second_turn, first_summary, third_turn, second_summary, *later = model.requests
    assert len(second_turn) == len(first_turn) + 2
    # The summary sees what is being removed, clipped, and builds on the last checkpoint.
    assert 'Call write_file: {"path": "Orders.java"' in first_summary[1].content
    assert 'Stock.java' not in first_summary[1].content
    assert len(first_summary[1].content) < 2000
    assert second_summary[1].content.startswith(
        'Previous checkpoint:\nDecisions: money is stored in cents\nRemaining: Payments')
    assert 'Call write_file: {"path": "Stock.java"' in second_summary[1].content

    for request, files, remaining in [
            (third_turn, 'Orders.java, Stock.java', 'Payments, Dockerfile'),
            (later[-1], 'Orders.java, Stock.java, Payments.java', 'Dockerfile')]:
        system, inputs, checkpoint, *recent = request
        assert isinstance(system, SystemMessage) and system.content == profile.prompt + runtime.ASK_GUIDANCE
        assert isinstance(inputs, HumanMessage) and inputs.content == 'requirements'
        assert isinstance(checkpoint, HumanMessage)
        assert f'Files written or edited so far: {files}\n' in checkpoint.content
        assert checkpoint.content.endswith(f'Remaining: {remaining}')
        assert_calls_and_results_correlate(recent)
    assert [call['id'] for call in third_turn[3].tool_calls] == ['Stock.java']
    results = {message.tool_call_id: message.content for message in later[-1]
               if isinstance(message, ToolMessage)}
    assert set(results) == {'Payments.java', 'missing', 'docker'}
    assert results['missing'].startswith('Tool failed: FileNotFoundError:')
    # Removed history costs nothing further, and the work it did is still on disk.
    assert sum(len(str(message.content)) for message in later[-1]) < len(source) * 2
    assert (profile.root / 'Orders.java').read_text() == source
    assert capsys.readouterr().out.count('Compacted 2 messages into a checkpoint') == 2


@pytest.mark.parametrize('budget,compacted', [('64000', True), ('0', False)])
def test_compaction_follows_provider_reported_usage(profile, scripted_model, monkeypatch,
                                                    budget, compacted):
    monkeypatch.setenv('AGENT_CONTEXT_TOKENS', budget)
    usage = {'input_tokens': 60000, 'output_tokens': 20, 'total_tokens': 60020}
    # By size alone this history is well inside the budget; the provider's count decides.
    model = scripted_model([
        write('first.txt', 'x' * 60000),
        AIMessage(content='', usage_metadata=usage, tool_calls=write('Dockerfile').tool_calls),
        *([AIMessage(content='Remaining: nothing')] if compacted else []),
        AIMessage(content='Finished'),
    ])
    runtime.run_agent(profile)
    assert len(model.requests) == (4 if compacted else 3)
    assert_calls_and_results_correlate(model.messages[2:])
    assert [type(message) for message in model.messages[2:]] == (
        [HumanMessage, AIMessage, ToolMessage] if compacted
        else [AIMessage, ToolMessage, AIMessage, ToolMessage])


def test_contract_files_are_read_and_searched_but_never_written(tmp_path):
    workspace = tmp_path / 'tests'
    supplied = tmp_path / 'contracts'
    workspace.mkdir()
    supplied.mkdir()
    (supplied / 'openapi.yaml').write_text('paths:\n  /orders:\n    post:\n      summary: Create\n')
    (tmp_path / 'secret.txt').write_text('private data')
    assert 'read_contract' not in {item.name for item in runtime.workspace_tools(workspace)}
    tools = {item.name: item for item in runtime.workspace_tools(workspace, supplied)}
    assert tools['read_contract'].invoke({'name': 'openapi.yaml', 'offset': 2, 'limit': 1}) == (
        '  /orders:\n\n[Showing lines 2-2 of 4. Continue with offset=3.]')
    assert tools['search_contracts'].invoke({'regex': 'summary'}) == (
        'openapi.yaml:4: summary: Create')
    assert tools['search_contracts'].invoke({'regex': 'private'}) == 'No matches'
    for name, args in [('read_contract', {'name': '../secret.txt'}),
                       ('search_contracts', {'regex': 'private', 'name': '..'}),
                       ('read_file', {'path': '../contracts/openapi.yaml'}),
                       ('write_file', {'path': '../contracts/openapi.yaml', 'content': ''})]:
        with pytest.raises(ValueError, match='Path must be inside'):
            tools[name].invoke(args)
    assert tools['list_files'].invoke({}) == 'No files found'


@pytest.mark.parametrize('prepare_inputs', [
    partial(implementation.prepare_inputs, get_language('java')),
    partial(test_generation.prepare_inputs, get_language('java')),
])
def test_large_contracts_are_indexed_and_supplied_whole_on_demand(tmp_path, monkeypatch,
                                                                  prepare_inputs):
    (tmp_path / 'server').mkdir()
    requirements = [{'id': f'FR-{number}', 'category': 'functional', 'title': f'Rule {number}',
                     'description': f'Detailed behaviour {number}. ' * 20, 'priority': 'must',
                     'rationale': f'Reason {number}'} for number in range(1, 31)]
    api = {
        'info': {'title': 'Orders', 'version': '1.0.0', 'description': '', 'auth': 'bearer'},
        'schemas': [{'name': 'Order', 'description': 'A placed order', 'properties': [
            {'name': 'total', 'type': 'integer', 'required': True, 'array': False}]}],
        'endpoints': [{'method': 'POST', 'path': '/orders', 'summary': 'Create an order',
                       'description': 'Totals are in cents.', 'tag': 'orders', 'parameters': [],
                       'request_schema': 'Order', 'response_status': 201,
                       'response_schema': 'Order', 'response_array': False,
                       'error_statuses': [400, 409], 'requirement_ids': ['FR-1', 'FR-2']}],
    }
    context = {'requirements': requirements, 'api_design': api,
               'requirements_document': render_requirements_markdown(
                   requirements, 'Orders are never deleted.')}

    def prepared(workspace):
        inputs = prepare_inputs(tmp_path, context)
        workspace.mkdir()
        inputs.populate(workspace)
        return inputs.context

    embedded = prepared(tmp_path / 'embedded')
    assert embedded['requirements'] == requirements
    assert not (tmp_path / 'embedded/contracts').exists()

    monkeypatch.setattr(contracts, 'CONTRACT_EMBED_CHARS', 2000)
    index = prepared(tmp_path / 'on-demand')
    supplied = tmp_path / 'on-demand/contracts'
    assert len(json.dumps(index)) < len(json.dumps(embedded)) / 3
    # Nothing is lost: the index names everything and the files hold all of it.
    assert index['requirements'] == [{'id': item['id'], 'category': 'functional',
                                      'priority': 'must', 'title': item['title']}
                                     for item in requirements]
    assert index['api'] == {'title': 'Orders', 'auth': 'bearer', 'schemas': ['Order'],
                            'endpoints': [{'operation': 'POST /orders',
                                           'summary': 'Create an order',
                                           'requirements': ['FR-1', 'FR-2']}]}
    assert sorted(index['contract_files']) == sorted(item.name for item in supplied.iterdir())
    assert json.loads((supplied / 'requirements.json').read_text()) == requirements
    assert (supplied / 'openapi.yaml').read_text() == render_openapi(api)
    assert (supplied / 'requirements.md').read_text() == 'Orders are never deleted.'
    assert not (tmp_path / 'on-demand/openapi.yaml').exists()
    assert not any(supplied.is_relative_to(project) for project in
                   (tmp_path / 'on-demand/service', tmp_path / 'on-demand/tests'))
    tools = {item.name: item for item in runtime.workspace_tools(tmp_path, supplied)}
    assert tools['search_contracts'].invoke({'regex': '"FR-17"'}).startswith(
        'requirements.json:')


@pytest.fixture
def tools(tmp_path):
    return {item.name: item
            for item in runtime.workspace_tools(tmp_path, build_dirs=['target'])}


@pytest.mark.parametrize('name,args', [
    ('read_file', {}),
    ('write_file', {'content': 'overwrite'}),
    ('edit_file', {'old_text': 'private', 'new_text': 'overwrite'}),
    ('search_files', {'regex': 'private'}),
    ('list_files', {}),
])
@pytest.mark.parametrize('escape', ['relative', 'absolute', 'symlink'])
def test_file_tools_reject_workspace_escapes(tmp_path, name, args, escape):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    private = tmp_path / 'private'
    private.mkdir()
    secret = private / 'secret.txt'
    secret.write_text('private data')
    (workspace / 'linked').symlink_to(private, target_is_directory=True)
    path = {'relative': '../private/secret.txt', 'absolute': str(secret),
            'symlink': 'linked/secret.txt'}[escape]
    tools = {item.name: item for item in runtime.workspace_tools(workspace)}
    with pytest.raises(ValueError, match='inside the project workspace'):
        tools[name].invoke({'path': path, **args})
    assert secret.read_text() == 'private data'


def test_discovery_tools_do_not_follow_links_out_of_the_workspace(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (tmp_path / 'secret.txt').write_text('private data')
    (workspace / 'linked.txt').symlink_to(tmp_path / 'secret.txt')
    (workspace / 'linked').symlink_to(tmp_path, target_is_directory=True)
    (workspace / 'own.txt').write_text('private notes')
    tools = {item.name: item for item in runtime.workspace_tools(workspace)}
    assert tools['list_files'].invoke({}) == 'own.txt'
    assert tools['search_files'].invoke({'regex': 'private'}) == 'own.txt:1: private notes'


def test_large_files_are_read_completely_in_continued_ranges(tmp_path, tools):
    lines = [f'line {number} ' + 'x' * 90 for number in range(1, 1201)]
    (tmp_path / 'large.txt').write_text('\n'.join(lines) + '\n')
    collected, offset, reads = [], 1, 0
    while offset:
        result = tools['read_file'].invoke({'path': 'large.txt', 'offset': offset})
        assert len(result) <= 30200
        body, _, note = result.rpartition('\n[Showing lines ')
        collected += body.splitlines()
        offset = int(note.split('offset=')[1].rstrip('.]')) if 'offset=' in note else 0
        reads += 1
    assert collected == lines and reads > 1
    assert tools['read_file'].invoke({'path': 'large.txt', 'offset': 7, 'limit': 2}) == (
        f'{lines[6]}\n{lines[7]}\n\n[Showing lines 7-8 of 1200. Continue with offset=9.]'
    )
    (tmp_path / 'small.txt').write_text('whole\nfile\n')
    assert tools['read_file'].invoke({'path': 'small.txt'}) == 'whole\nfile\n'
    (tmp_path / 'minified.js').write_text('x' * 40000)
    result = tools['read_file'].invoke({'path': 'minified.js'})
    assert result.startswith('x' * 30000 + '\n[Line 1 truncated')


def test_edit_file_changes_only_an_unambiguous_exact_match(tmp_path, tools):
    source = tmp_path / 'Service.java'
    source.write_text('int a = 1;\nint b = 1;\nreturn a;\n')
    assert tools['edit_file'].invoke({
        'path': 'Service.java', 'old_text': 'return a;', 'new_text': 'return a + b;',
    }) == 'Edited (1 replacement)'
    assert source.read_text() == 'int a = 1;\nint b = 1;\nreturn a + b;\n'
    for old_text, error in [(' = 1;', 'matches 2 places'), ('missing', 'was not found'),
                            ('', 'was not found')]:
        with pytest.raises(ValueError, match=error):
            tools['edit_file'].invoke({'path': 'Service.java', 'old_text': old_text,
                                       'new_text': ' = 2;'})
    assert source.read_text() == 'int a = 1;\nint b = 1;\nreturn a + b;\n'
    assert tools['edit_file'].invoke({
        'path': 'Service.java', 'old_text': ' = 1;', 'new_text': ' = 2;', 'replace_all': True,
    }) == 'Edited (2 replacements)'
    assert source.read_text() == 'int a = 2;\nint b = 2;\nreturn a + b;\n'


def test_files_are_located_without_reading_build_output(tmp_path, tools):
    for path, content in [('src/main/java/OrderController.java', 'class OrderController {}'),
                          ('src/main/java/Order.java', 'class Order {}'),
                          ('target/classes/Order.java', 'class Order {}'),
                          ('pom.xml', '<project/>')]:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(content)
    assert tools['list_files'].invoke({}).splitlines() == [
        'pom.xml', 'src/main/java/Order.java', 'src/main/java/OrderController.java']
    assert tools['list_files'].invoke({'pattern': '*Controller.java'}) == (
        'src/main/java/OrderController.java')
    assert tools['list_files'].invoke({'path': 'src', 'pattern': '*.xml'}) == 'No files found'
    assert tools['search_files'].invoke({'regex': r'class Order\b', 'pattern': '*.java'}) == (
        'src/main/java/Order.java:1: class Order {}')
    assert tools['search_files'].invoke({'regex': 'absent'}) == 'No matches'
    for number in range(300):
        (tmp_path / f'src/Generated{number:03}.java').write_text('class Generated {}\n' * 2)
    listed = tools['list_files'].invoke({'path': 'src'}).splitlines()
    assert len(listed) == 201 and listed[-1].startswith('[More than 200 files')
    found = tools['search_files'].invoke({'regex': 'Generated'}).splitlines()
    assert len(found) == 51 and found[-1].startswith('[More than 50 matches')


def test_shell_runs_in_project_and_returns_short_output_whole(tmp_path, tools):
    script = 'import os, sys; print(os.getcwd()); sys.exit(3)'
    result = tools['shell'].invoke({
        'command': f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}',
    })
    assert result == f'Exit code: 3\n{tmp_path.resolve()}\n'
    assert tools['list_files'].invoke({'path': '.agent-output'}) == 'No files found'


def test_long_shell_output_is_summarized_and_remains_retrievable(tmp_path, tools):
    script = ('for n in range(1, 2001):\n'
              '    print("[ERROR] Service.java:[12,5] cannot find symbol" if n == 700'
              '          else f"[INFO] downloading artifact {n}")\n'
              'print("BUILD FAILURE")')
    result = tools['shell'].invoke({
        'command': f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}',
    })
    assert len(result) < 5000
    assert result.startswith('Exit code: 0\n[Output was 2001 lines; the full log is '
                             '.agent-output/shell-1.log.')
    assert '700: [ERROR] Service.java:[12,5] cannot find symbol' in result
    assert result.endswith('[INFO] downloading artifact 2000\nBUILD FAILURE\n')
    assert 'artifact 100\n' not in result
    assert tools['read_file'].invoke({
        'path': '.agent-output/shell-1.log', 'offset': 699, 'limit': 3,
    }).startswith('[INFO] downloading artifact 699\n[ERROR] Service.java:[12,5] cannot find '
                  'symbol\n[INFO] downloading artifact 701\n')
    assert tools['search_files'].invoke({
        'regex': 'artifact 100$', 'path': '.agent-output/shell-1.log',
    }) == '.agent-output/shell-1.log:100: [INFO] downloading artifact 100'
    # Saved logs never surface in ordinary project discovery.
    assert tools['list_files'].invoke({}) == 'No files found'
    assert tools['search_files'].invoke({'regex': 'artifact'}) == 'No matches'


async def test_paused_stage_keeps_its_workspace_until_a_new_container_continues(tmp_path, monkeypatch):
    questions = ASK['args']['questions']
    populated = []

    def prepare_inputs(session_dir, context):
        def populate(workspace):
            populated.append(workspace)
            (workspace / 'notes').mkdir()
        return StageInputs(context={}, populate=populate)

    def sanitize_output(source, target):
        copy_regular_files(source, target, include=lambda relative: True)

    stage = StageSpec(
        role='documentation', artifact_subdir='docs-stage', workspace_name='notes',
        output_subdir='published', heartbeat='Authoring docs', image_label=None,
        image_build=docker_agents.AgentImageBuild(tmp_path, tmp_path / 'Dockerfile'),
        prepare_inputs=prepare_inputs, sanitize_output=sanitize_output,
        validate_output=lambda clean: None,
    )
    sent: list[dict[str, bytes]] = []
    exit_codes = iter([runtime.PAUSE_EXIT, 0])

    async def docker(*args, **kwargs):
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.append({item.name: archive.extractfile(item).read()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': next(exit_codes)})
        if args[0] == 'cp' and args[1].endswith(':/workspace/.'):
            # What the agent left behind when it paused.
            (Path(args[2]) / 'notes').mkdir()
            (Path(args[2]) / 'notes/draft.md').write_text('# Draft')
            (Path(args[2]) / runtime.STATE_FILENAME).write_text(
                json.dumps({'pending': {'call_id': 'ask', 'questions': questions}}))
        if args[0] == 'cp' and args[1].endswith(':/workspace/notes/.'):
            (Path(args[2]) / 'index.md').write_text('# Published')
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    environment = ActivityEnvironment()
    result = await environment.run(run_stage, stage, 'session', {}, tmp_path)
    assert result == {'session_id': 'session', 'questions': questions}
    paused = tmp_path / 'session/docs-stage' / docker_agents.PAUSED_DIR
    assert (paused / 'notes/draft.md').read_text() == '# Draft'

    result = await environment.run(run_stage, stage, 'session', {'answers': ['EUR']}, tmp_path)
    assert len(populated) == 1
    resumed = sent[1]
    assert resumed['notes/draft.md'] == b'# Draft'
    assert json.loads(resumed[runtime.ANSWERS_FILENAME]) == {'answers': ['EUR']}
    assert (Path(result['path']) / 'index.md').read_text() == '# Published'
    assert not paused.exists()
    # Nothing is left to continue from.
    with pytest.raises(ApplicationError, match='paused agent run is unavailable'):
        await environment.run(run_stage, stage, 'session', {'answers': ['EUR']}, tmp_path)


async def test_new_stage_uses_its_own_toolchain_workspace_and_artifact_names(tmp_path, monkeypatch):
    """Extension contract: a non-Maven stage needs policies, not runner modifications."""
    image_context = tmp_path / 'python-author'
    image_context.mkdir()
    dockerfile = image_context / 'Dockerfile'
    dockerfile.write_text('FROM python:3.12-slim')
    agent_builds = []

    def prepare_inputs(session_dir, context):
        def populate(workspace):
            project = workspace / 'notes'
            project.mkdir()
            (project / 'source.txt').write_text('author input')
        return StageInputs(context={'purpose': 'documentation'}, populate=populate)

    def sanitize_output(source, target):
        copy_regular_files(source, target, include=lambda relative: relative.suffix == '.md')
        (target / 'Dockerfile').write_text('FROM scratch\nCOPY . /docs')

    def validate_output(clean):
        if not (clean / 'index.md').is_file():
            raise RuntimeError('Missing documentation')

    stage = StageSpec(
        role='documentation', artifact_subdir='docs-stage', workspace_name='notes',
        output_subdir='published', heartbeat='Authoring docs', image_label='documentation',
        image_build=docker_agents.AgentImageBuild(image_context, dockerfile, {'TOOLCHAIN': 'python'}),
        prepare_inputs=prepare_inputs, sanitize_output=sanitize_output, validate_output=validate_output,
    )

    async def docker(*args, **kwargs):
        if args[0] == 'build' and '--iidfile' not in args:
            agent_builds.append(args)
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                assert archive.extractfile('notes/source.txt').read() == b'author input'
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1].endswith(':/workspace/notes/.'):
            (Path(args[2]) / 'index.md').write_text('# Published docs')
        if args[0] == 'build' and '--iidfile' in args:
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'd' * 64)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    result = await ActivityEnvironment().run(run_stage, stage, 'session', {}, tmp_path)
    build, = agent_builds
    assert build[-1] == str(image_context)
    assert build[build.index('-f') + 1] == str(dockerfile)
    assert build[build.index('--build-arg') + 1] == 'TOOLCHAIN=python'
    destination = tmp_path / 'session/docs-stage/published'
    assert result['path'] == str(destination)
    assert (destination / 'index.md').read_text() == '# Published docs'
