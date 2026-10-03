import json
import shlex
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.agent_workflows import runtime
from app.agent_workflows.host import StageInputs, StageSpec, copy_regular_files, run_stage
from app.implementation.agent import validate_completion as validate_implementation
from app.test_generation.agent import validate_completion as validate_tests


@pytest.fixture
def scripted_model(monkeypatch):
    """Replace only the external provider; tools and completion checks remain real."""
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://provider.example/v1')
    monkeypatch.delenv('TEST_AGENT_MAX_TURNS', raising=False)

    def install(responses):
        class Model:
            def __init__(self):
                self.responses = iter(responses)
                self.messages = []

            def bind_tools(self, tools):
                return self

            def invoke(self, messages):
                self.messages = list(messages)
                return next(self.responses)

        model = Model()
        monkeypatch.setattr(runtime, 'create_model', lambda **settings: model)
        return model

    return install


@pytest.fixture
def profile(tmp_path):
    return runtime.AgentProfile(
        root=tmp_path, prompt='Implement the supplied contract', initial_messages=['requirements'],
        max_turns_env='TEST_AGENT_MAX_TURNS', validate_completion=validate_implementation,
        completion_message='Completed', exhaustion_message='Tool-turn limit exceeded',
    )


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
    (validate_tests, 'without test classes'),
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


@pytest.mark.parametrize('name', ['read_file', 'write_file'])
@pytest.mark.parametrize('escape', ['relative', 'absolute', 'symlink'])
def test_file_tools_reject_workspace_escapes(tmp_path, name, escape):
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
    args = {'path': path}
    if name == 'write_file':
        args['content'] = 'overwrite'
    with pytest.raises(ValueError, match='inside the project workspace'):
        tools[name].invoke(args)
    assert secret.read_text() == 'private data'


def test_workspace_tools_bound_outputs_and_use_project_cwd(tmp_path):
    tools = {item.name: item for item in runtime.workspace_tools(tmp_path)}
    (tmp_path / 'large.txt').write_text('x' * 40000)
    assert len(tools['read_file'].invoke({'path': 'large.txt'})) == 30000
    script = 'import os; print("x" * 20000); print(os.getcwd())'
    result = tools['shell'].invoke({
        'command': f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}',
    })
    header, output = result.split('\n', 1)
    assert header == 'Exit code: 0'
    assert len(output.encode()) <= 16000
    assert output.endswith(str(tmp_path.resolve()) + '\n')


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
