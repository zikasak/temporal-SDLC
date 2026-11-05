"""Routing a change's criteria to the suite that can prove them, and the test author's
finding that a criterion has none."""
import dataclasses
import json
import re
import tarfile
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.api_design.models import empty_api
from app.languages import get_language
from app.routing import activities
from app.routing.models import ROUTING_ERROR, added_seams, judge_routes, read_seam
from app.routing.parts import SERVICE_PART, ComponentPart
from app.test_generation.activities import read_unobservable

REQUIREMENT = {'category': 'functional', 'title': 'T', 'description': 'D', 'priority': 'must'}
BRIEF = {'requirements': [{**REQUIREMENT, 'id': 'FR-2'}, {**REQUIREMENT, 'id': 'FR-3'}],
         'requirements_document': '# Brief', 'retired': []}
CONTEXT = {'requirements': [], 'requirements_document': '# Contract', 'language': 'java',
           'api_design': empty_api(), 'brief': BRIEF,
           'repository': {'full_name': 'octo/hotel'}}
ROUTES = {'routes': [
    {'requirement_id': 'FR-2', 'route': 'contract', 'reason': 'POST /rooms, then GET.'},
    {'requirement_id': 'FR-3', 'route': 'unobservable', 'reason': '  Needs a merged\n pull request. '},
]}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    session_dir = tmp_path / 'session'
    for path, text in {
        'repository/Dockerfile': 'FROM eclipse-temurin',
        'repository/src/main/java/Rooms.java': 'SERVICE SOURCE',
        'repository/contract-tests/README.md': 'How the suite runs',
        'repository/.git/config': 'GIT DATA',
        'repository/target/rooms.jar': 'BUILD OUTPUT',
        'tests/suite/RoomsTest.java': 'NOT THE REPOSITORY',
    }.items():
        (session_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (session_dir / path).write_text(text)
    return session_dir


# Captured at import, before the autouse fixture stands in for it.
CHECK_OPENAI_CREDENTIALS = docker_agents.check_openai_credentials


def fake_docker(monkeypatch, routes, sent: dict):
    """Stands in for the Docker CLI and the external agent's filesystem only."""
    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/routing/.') and routes is not None:
            (Path(args[2]) / 'routes.json').write_text(
                routes if isinstance(routes, str) else json.dumps(routes))
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)


async def test_routing_reads_the_repository_and_returns_one_route_per_criterion(session, monkeypatch):
    sent: dict = {}
    fake_docker(monkeypatch, ROUTES, sent)
    result = await ActivityEnvironment().run(activities.route_requirements, 'session', CONTEXT)
    assert result == {'session_id': 'session', 'path': str(session / 'routing'), 'routes': {
        'FR-2': {'route': 'contract', 'reason': 'POST /rooms, then GET.'},
        'FR-3': {'route': 'unobservable', 'reason': 'Needs a merged pull request.'}},
        'parts': []}
    assert json.loads((session / 'routing/routes.json').read_text()) == result
    payload = json.loads(sent.pop('input.json'))
    assert payload['brief'] == BRIEF and payload['api_design']
    assert 'repository' not in payload and 'language' not in payload
    # The router sees the repository as it is, without its Git data or build output, and
    # nothing the delivery's authors wrote.
    assert {name: text for name, text in sent.items() if name.startswith('routing/')} == {
        'routing/repository/Dockerfile': 'FROM eclipse-temurin',
        'routing/repository/src/main/java/Rooms.java': 'SERVICE SOURCE',
        'routing/repository/contract-tests/README.md': 'How the suite runs'}


@pytest.mark.parametrize('routes, error', [
    (None, 'did not produce routes'),
    ('not json', 'not valid JSON'),
    ([], 'not a list of routes'),
    ({'routes': ROUTES['routes'][:1]}, 'without a route: FR-3'),
    ({'routes': [*ROUTES['routes'], ROUTES['routes'][0]]}, 'routed twice'),
    ({'routes': [*ROUTES['routes'], {**ROUTES['routes'][0], 'requirement_id': 'FR-9'}]},
     'does not have: FR-9'),
    ({'routes': [ROUTES['routes'][0], {**ROUTES['routes'][1], 'route': 'elsewhere'}]},
     'no valid route'),
    ({'routes': [ROUTES['routes'][0], {**ROUTES['routes'][1], 'route': 'component'}]},
     'names no seam module'),
    ({'routes': [ROUTES['routes'][0], {**ROUTES['routes'][1], 'reason': ' '}]}, 'no reason'),
])
async def test_an_unusable_routing_routes_nothing(session, monkeypatch, routes, error):
    fake_docker(monkeypatch, routes, {})
    with pytest.raises(ApplicationError, match=error):
        await ActivityEnvironment().run(activities.route_requirements, 'session', CONTEXT)
    assert not (session / 'routing/routes.json').exists()


async def test_routing_needs_the_checkout_and_a_brief(session, monkeypatch):
    fake_docker(monkeypatch, ROUTES, {})
    with pytest.raises(ApplicationError, match='no requirements'):
        await ActivityEnvironment().run(activities.route_requirements, 'session',
                                        {**CONTEXT, 'brief': {**BRIEF, 'requirements': []}})
    with pytest.raises(ApplicationError, match='checkout is unavailable'):
        await ActivityEnvironment().run(activities.route_requirements, 'other', CONTEXT)


async def test_a_routing_model_without_credentials_says_how_to_fix_it(session, monkeypatch):
    fake_docker(monkeypatch, ROUTES, {})
    # The real credential check, not the fixture's stand-in.
    monkeypatch.setattr(docker_agents, 'check_openai_credentials', CHECK_OPENAI_CREDENTIALS)
    monkeypatch.setattr(docker_agents, 'OPENAI_API_KEY', '')

    async def settings(role):
        return docker_agents.ModelSettings(adapter='openai', model='gpt-test')

    monkeypatch.setattr(docker_agents, 'get_workflow_settings', settings)
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(activities.route_requirements, 'session', CONTEXT)
    assert raised.value.type == ROUTING_ERROR and raised.value.non_retryable
    assert 'OPENAI_API_KEY is not set' in raised.value.message
    assert 'choose another model for the Routing stage in Settings' in raised.value.message


def test_unobservable_criteria_are_named_with_their_reasons():
    routes = {f'FR-{number}': {'route': 'unobservable', 'reason': f'R{number}.',
                               'source': 'routing'} for number in range(2, 9)}
    routes['FR-1'] = {'route': 'contract', 'reason': 'GET /rooms.', 'source': 'routing'}
    status, error = judge_routes(routes)
    assert status == 'unobservable'
    assert error.startswith('The contract tests cannot observe 7 of the 8 acceptance criteria')
    assert 'FR-2: R2.; FR-3: R3.' in error and 'FR-6: R6.; 2 more' in error
    assert 'FR-7' not in error
    assert judge_routes({'FR-1': routes['FR-1']}) == ('routed', None)


def test_the_test_author_may_name_criteria_of_the_brief_instead_of_writing_a_suite(tmp_path):
    ids = ['FR-2', 'FR-3']
    # A suite without the file is published as usual.
    assert read_unobservable(ids, tmp_path) is None
    file = tmp_path / 'unobservable.json'
    file.write_text(json.dumps({'unobservable': [
        {'requirement_id': 'FR-3', 'reason': 'Needs a pull request.'}]}))
    assert read_unobservable(ids, tmp_path) == {'unobservable': [
        {'requirement_id': 'FR-3', 'reason': 'Needs a pull request.'}]}
    for content, error in [
        ('[]', 'names no criterion'), ('{"unobservable": []}', 'names no criterion'),
        ('nope', 'not valid JSON'),
        (json.dumps({'unobservable': [{'requirement_id': 'FR-9', 'reason': 'R'}]}),
         'does not have: FR-9'),
        (json.dumps({'unobservable': [{'requirement_id': 'FR-2', 'reason': ''}]}), 'no reason'),
    ]:
        file.write_text(content)
        with pytest.raises(RuntimeError, match=error):
            read_unobservable(ids, tmp_path)


# A Python service whose criterion FR-3 is observable only through its code.
SERVICE = '''class Rooms:
    def close(self, room: str, merged: bool) -> None:
        self._closed = merged

    def state(self, room: str) -> str:
        return "closed"
'''
STUB = '''class Rooms:
    def close(self, room: str, merged: bool) -> None:
        """Closes the room, whether its pull request was merged or not."""
        ...

    def state(self, room: str) -> str: ...
'''
RUNTIME = {'source_root': 'src', 'requirements': ['requirements.txt']}
COMPONENT_ROUTES = {'routes': [
    ROUTES['routes'][0],
    {'requirement_id': 'FR-3', 'route': 'component', 'reason': 'Close, then read the state.',
     'seam': {'module': 'service.rooms', 'symbols': ['Rooms.close', 'Rooms.state']}},
]}
PYTHON_CONTEXT = {**CONTEXT, 'language': 'python'}


@pytest.fixture
def python_session(session):
    for path, text in {'repository/src/service/rooms.py': SERVICE,
                       'repository/requirements.txt': 'temporalio\n'}.items():
        (session / path).parent.mkdir(parents=True, exist_ok=True)
        (session / path).write_text(text)
    return session


def fake_component_docker(monkeypatch, components: dict[str, str], runs: list,
                          routes=COMPONENT_ROUTES, conforms: bool = True):
    """Stands in for the Docker CLI, the agent's filesystem and stubtest's verdict."""
    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1].endswith(':/workspace/routing/.'):
            output = Path(args[2])
            (output / 'routes.json').write_text(json.dumps(routes))
            for path, text in components.items():
                (output / 'components' / path).parent.mkdir(parents=True, exist_ok=True)
                (output / 'components' / path).write_text(text)
        if args[0] == 'build' and '--iidfile' in args:
            context = Path(args[-1])
            runs.append({'build': args, 'files': sorted(
                item.relative_to(context).as_posix() for item in context.rglob('*')
                if item.is_file())})
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'a' * 64)
        if args[0] == 'run':
            runs.append({'run': args})
            if not conforms:
                raise RuntimeError('Docker run failed: error: service.rooms.Rooms.close is '
                                   'inconsistent, stub parameter "merged" differs')
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)


async def test_a_criterion_observable_at_a_seam_is_routed_there_with_its_checked_stubs(
        python_session, monkeypatch):
    runs: list = []
    fake_component_docker(monkeypatch, {'runtime.json': json.dumps(RUNTIME),
                                        'service/rooms.pyi': STUB}, runs)
    result = await ActivityEnvironment().run(activities.route_requirements, 'session',
                                             PYTHON_CONTEXT)
    assert result['routes']['FR-3'] == {
        'route': 'component', 'reason': 'Close, then read the state.',
        'seam': {'module': 'service.rooms', 'symbols': ['Rooms.close', 'Rooms.state']}}
    components = python_session / 'routing/components'
    assert result['components'] == str(components)
    assert (components / 'service/rooms.pyi').read_text() == STUB
    build, run = runs
    # The check sees the service's code, not its tests or contracts, and the stubs.
    assert {'repository/src/service/rooms.py', 'repository/requirements.txt',
            'components/service/rooms.pyi', 'Dockerfile', 'check.sh'} <= set(build['files'])
    assert not any(name.startswith('repository/contract-tests') for name in build['files'])
    assert 'SOURCE_ROOT=src' in build['build'] and 'REQUIREMENTS=requirements.txt' in build['build']
    assert '--network=none' in run['run'] and run['run'][-1] == 'service.rooms'


VALID = {'runtime.json': json.dumps(RUNTIME), 'service/rooms.pyi': STUB}


@pytest.mark.parametrize('components, published, conforms, error', [
    ({'service/rooms.pyi': STUB}, {}, True, 'no runtime.json'),
    ({**VALID, 'runtime.json': json.dumps({**RUNTIME, 'source_root': '../elsewhere'})}, {},
     True, 'not a path inside the repository'),
    ({**VALID, 'runtime.json': json.dumps({**RUNTIME, 'requirements': ['missing.txt']})}, {},
     True, 'missing.txt is not a file of the repository'),
    ({**VALID, 'service/rooms.pyi': STUB.replace('        ...\n',
                                                 '        self._closed = merged\n')},
     {}, True, 'more than a docstring'),
    ({**VALID, 'service/rooms.pyi': STUB + 'close = lambda room: None\n'}, {}, True,
     'holds no code'),
    ({**VALID, 'service/rooms.pyi': STUB.replace('def state', 'def status')}, {}, True,
     'does not define: Rooms.state'),
    (VALID, {'service/rooms.pyi': STUB.replace('str: ...', 'int: ...')}, True,
     'published stub service/rooms.pyi was changed'),
    (VALID, {'service/other.pyi': 'def other() -> None: ...\n'}, True, 'was removed'),
    (VALID, {}, False, 'does not match the code: error: service.rooms.Rooms.close'),
])
async def test_a_component_contract_that_is_not_a_checked_description_of_the_code_routes_nothing(
        python_session, monkeypatch, components, published, conforms, error):
    for path, text in published.items():
        file = python_session / 'repository/contracts/components' / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
    fake_component_docker(monkeypatch, components, [], conforms=conforms)
    with pytest.raises(ApplicationError, match=error):
        await ActivityEnvironment().run(activities.route_requirements, 'session',
                                        PYTHON_CONTEXT)
    assert not (python_session / 'routing/routes.json').exists()
    assert not (python_session / 'routing/components').exists()


async def test_the_agent_extends_the_published_contract_and_is_told_how(
        python_session, monkeypatch):
    published = 'def opened(room: str) -> bool: ...\n'
    file = python_session / 'repository/contracts/components/service/rooms.pyi'
    file.parent.mkdir(parents=True)
    file.write_text(published)
    fake_component_docker(monkeypatch, {**VALID, 'service/rooms.pyi': published + STUB}, [])
    sent: dict = {}
    fake = docker_agents.docker

    async def docker(*args, **kwargs):
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
            return ''
        return await fake(*args, **kwargs)

    monkeypatch.setattr(docker_agents, 'docker', docker)
    result = await ActivityEnvironment().run(activities.route_requirements, 'session',
                                             PYTHON_CONTEXT)
    assert result['routes']['FR-3']['route'] == 'component'
    assert sent['routing/components/service/rooms.pyi'] == published
    assert 'Component contract (Python)' in json.loads(sent['language.json'])['components']


def test_a_language_without_component_contracts_routes_to_no_seam(tmp_path):
    language = dataclasses.replace(get_language('java'), component_contract=None)
    (tmp_path / 'routes.json').write_text(json.dumps(COMPONENT_ROUTES))
    with pytest.raises(RuntimeError, match='FR-3 has no valid route; the routes are '
                                           'contract, unobservable'):
        activities.read_output(tmp_path, ['FR-2', 'FR-3'], language,
                               [ComponentPart(SERVICE_PART, '.', language)])


JAVA_STUB = """package com.example.rooms;

import io.temporal.workflow.SignalMethod;
import io.temporal.workflow.WorkflowInterface;

/** A room's workflow. */
@WorkflowInterface
public interface RoomWorkflow {
    /** Closes the room, whether its pull request was merged or not. */
    @SignalMethod
    void close(boolean merged);

    String state();
}
"""
JAVA_ROUTES = {'routes': [
    ROUTES['routes'][0],
    {'requirement_id': 'FR-3', 'route': 'component', 'reason': 'Signal, then query.',
     'seam': {'module': 'com.example.rooms.RoomWorkflow',
              'symbols': ['RoomWorkflow.close', 'RoomWorkflow.state']}},
]}


@pytest.fixture
def java_session(session):
    (session / 'repository/service/pom.xml').parent.mkdir(parents=True)
    (session / 'repository/service/pom.xml').write_text('<project/>')
    return session


async def test_a_java_seam_is_routed_with_its_stubs_checked_against_the_compiled_code(
        java_session, monkeypatch):
    runs: list = []
    fake_component_docker(monkeypatch, {
        'runtime.json': json.dumps({'project': 'service'}),
        'com/example/rooms/RoomWorkflow.java': JAVA_STUB}, runs, routes=JAVA_ROUTES)
    sent: dict = {}
    fake = docker_agents.docker

    async def docker(*args, **kwargs):
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
            return ''
        return await fake(*args, **kwargs)

    monkeypatch.setattr(docker_agents, 'docker', docker)
    result = await ActivityEnvironment().run(activities.route_requirements, 'session', CONTEXT)
    assert result['routes']['FR-3']['seam']['module'] == 'com.example.rooms.RoomWorkflow'
    components = java_session / 'routing/components'
    assert (components / 'com/example/rooms/RoomWorkflow.java').read_text() == JAVA_STUB
    # The agent is told how to write Java stubs and gets the trusted tool to check them.
    assert 'Component contract (Java)' in json.loads(sent['language.json'])['components']
    assert sent['tools/Stubs.java'].startswith('// Trusted tool of Java component contracts')
    build, run = runs
    assert {'repository/service/pom.xml', 'components/com/example/rooms/RoomWorkflow.java',
            'Dockerfile', 'build.sh', 'check.sh', 'Stubs.java'} <= set(build['files'])
    assert 'PROJECT=service' in build['build']
    assert '--network=none' in run['run'] and run['run'][-1] == 'com.example.rooms.RoomWorkflow'


@pytest.mark.parametrize('components, error', [
    ({'runtime.json': json.dumps({'project': '.'}),
      'com/example/rooms/RoomWorkflow.java': JAVA_STUB}, 'project . is not a directory of the '
                                                         'repository with a pom.xml'),
    ({'runtime.json': json.dumps({'project': 'service'}),
      'com/example/rooms/RoomWorkflow.java': JAVA_STUB.replace(
          'void close(boolean merged);', 'void close(boolean merged) { }')},
     'the body of close is more than `;`'),
    ({'runtime.json': json.dumps({'project': 'service'}),
      'com/example/other/RoomWorkflow.java': JAVA_STUB}, 'its package is com.example.rooms, '
                                                         'but its path is that of '
                                                         'com.example.other'),
])
async def test_a_java_component_contract_that_is_no_description_of_the_code_routes_nothing(
        java_session, monkeypatch, components, error):
    fake_component_docker(monkeypatch, components, [], routes=JAVA_ROUTES)
    with pytest.raises(ApplicationError, match=re.escape(error)):
        await ActivityEnvironment().run(activities.route_requirements, 'session', CONTEXT)
    assert not (java_session / 'routing/components').exists()


def test_criteria_component_tests_prove_are_routed():
    routes = {'FR-1': {'route': 'contract', 'reason': 'GET /rooms.', 'source': 'routing'},
              'FR-2': {'route': 'component', 'reason': 'Signal, then query.', 'source': 'routing',
                       'seam': {'module': 'app.workflows', 'symbols': ['Flow.close']}}}
    assert judge_routes(routes) == ('routed', None)
    # A delivery started before deliveries wrote component tests stops there, as it did.
    status, error = judge_routes(routes, component_tests=False)
    assert status == 'unsupported'
    assert error.startswith('1 of the 2 acceptance criteria can only be observed through the '
                            'code, not over HTTP (FR-2 at app.workflows)')
    # A criterion no suite can observe is the stop the user has to act on first.
    routes['FR-3'] = {'route': 'unobservable', 'reason': 'R.', 'source': 'routing'}
    assert judge_routes(routes)[0] == 'unobservable'
    assert judge_routes(routes, component_tests=False)[0] == 'unobservable'


# A seam the code does not have yet: `merged`, which the change adds first.
ADDED_STUB = STUB + '''
    def merged(self, room: str) -> None:
        """The room's pull request was merged."""
        ...
'''
ADDED_ROUTES = {'routes': [
    ROUTES['routes'][0],
    {'requirement_id': 'FR-3', 'route': 'component', 'reason': 'Merge, then read the state.',
     'seam': {'module': 'service.rooms', 'symbols': ['Rooms.merged', 'Rooms.state'],
              'added': ['Rooms.merged']}},
]}


async def test_a_seam_the_change_adds_first_is_routed_with_its_stubs_unchecked_until_added(
        python_session, monkeypatch):
    runs: list = []
    fake_component_docker(monkeypatch, {**VALID, 'service/rooms.pyi': ADDED_STUB}, runs,
                          routes=ADDED_ROUTES, conforms=False)
    result = await ActivityEnvironment().run(activities.route_requirements, 'session',
                                             PYTHON_CONTEXT)
    assert result['routes']['FR-3']['seam'] == {
        'module': 'service.rooms', 'symbols': ['Rooms.merged', 'Rooms.state'],
        'added': ['Rooms.merged']}
    assert added_seams(result['routes']) == {'service.rooms': ['Rooms.merged']}
    # The stubs are read and kept, but no code has the added symbols yet to check them by.
    assert (python_session / 'routing/components/service/rooms.pyi').read_text() == ADDED_STUB
    assert runs == []


@pytest.mark.parametrize('added, error', [
    (['Rooms.reopened'], 'does not define: Rooms.reopened'),
    ('Rooms.merged', 'names the symbols it adds other than as a list'),
    ([''], 'names the symbols it adds other than as a list'),
])
async def test_a_seam_the_change_adds_names_symbols_its_stub_declares(
        python_session, monkeypatch, added, error):
    routes = json.loads(json.dumps(ADDED_ROUTES))
    routes['routes'][1]['seam']['added'] = added
    fake_component_docker(monkeypatch, {**VALID, 'service/rooms.pyi': ADDED_STUB}, [],
                          routes=routes)
    with pytest.raises(ApplicationError, match=error):
        await ActivityEnvironment().run(activities.route_requirements, 'session',
                                        PYTHON_CONTEXT)


def test_the_seams_a_change_adds_are_collected_by_module():
    seam = {'module': 'app.workflows', 'symbols': ['Flow.close', 'Flow.state']}
    routes = {'FR-1': {'route': 'contract', 'reason': 'R.'},
              'FR-2': {'route': 'component', 'reason': 'R.', 'seam': seam},
              'FR-3': {'route': 'component', 'reason': 'R.',
                       'seam': {**seam, 'added': ['Flow.close']}},
              'FR-4': {'route': 'component', 'reason': 'R.',
                       'seam': {**seam, 'added': ['Flow.close', 'Flow.state']}}}
    assert added_seams(routes) == {'app.workflows': ['Flow.close', 'Flow.state']}
    assert added_seams({'FR-2': routes['FR-2']}) == {}
    # An empty list adds nothing, and is not kept.
    assert 'added' not in read_seam('FR-2', {**seam, 'added': []})


# A web client beside the Python service: criteria about it are observed at its own code.
FRONTEND = {
    'repository/frontend/package.json': json.dumps({'devDependencies': {'typescript': '^5'}}),
    'repository/frontend/package-lock.json': '{}',
    'repository/frontend/tsconfig.json': '{}',
    'repository/frontend/src/github.ts': 'export function createGitHubApp() {}',
    'repository/frontend/node_modules/typescript/package.json': '{}',
}
GITHUB_STUB = ('/** Subscribes the app to pull_request events. */\n'
               'export declare function createGitHubApp(name: string, organization: string): '
               'void;\n')
PART_CONTRACT = {'parts/frontend/components/runtime.json': json.dumps({'project': 'frontend'}),
                 'parts/frontend/components/src/github.d.ts': GITHUB_STUB}
PART_SEAM = {'part': 'frontend', 'module': 'src/github', 'symbols': ['createGitHubApp']}
PART_ROUTES = {'routes': [
    ROUTES['routes'][0],
    {'requirement_id': 'FR-3', 'route': 'component', 'reason': 'Read the posted manifest.',
     'seam': PART_SEAM},
]}


@pytest.fixture
def frontend_session(python_session):
    for path, text in FRONTEND.items():
        (python_session / path).parent.mkdir(parents=True, exist_ok=True)
        (python_session / path).write_text(text)
    return python_session


def fake_part_docker(monkeypatch, components, runs, sent, routes=PART_ROUTES):
    fake_component_docker(monkeypatch, {}, runs, routes=routes)
    fake = docker_agents.docker

    async def docker(*args, **kwargs):
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
            return ''
        result = await fake(*args, **kwargs)
        if args[0] == 'cp' and args[1].endswith(':/workspace/routing/.'):
            for path, text in components.items():
                (Path(args[2]) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(args[2]) / path).write_text(text)
        return result

    monkeypatch.setattr(docker_agents, 'docker', docker)


async def test_a_criterion_of_another_part_is_routed_to_its_seam(frontend_session, monkeypatch):
    published = 'export declare function takeAppReturn(): null;\n'
    file = frontend_session / 'repository/contracts/parts/frontend/components/src/app.d.ts'
    file.parent.mkdir(parents=True)
    file.write_text(published)
    runs: list = []
    sent: dict = {}
    fake_part_docker(monkeypatch, {**PART_CONTRACT,
                                   'parts/frontend/components/src/app.d.ts': published},
                     runs, sent)
    result = await ActivityEnvironment().run(activities.route_requirements, 'session',
                                             PYTHON_CONTEXT)
    # The router is told of the part, how to write its contract and how to check it, and
    # gets the part's published contract, but not its installed dependencies.
    assert json.loads(sent['input.json'])['parts'] == [
        {'name': 'service', 'root': '.', 'language': 'Python (FastAPI)'},
        {'name': 'frontend', 'root': 'frontend', 'language': 'TypeScript'}]
    language = json.loads(sent['language.json'])
    assert 'Component contract (Python)' in language['components']
    assert language['part_components']['typescript'].startswith(
        'Component contract (TypeScript) of a part')
    assert sent['tools/check.mjs'].startswith('// Trusted tool of TypeScript component')
    assert sent['routing/parts/frontend/components/src/app.d.ts'] == published
    assert 'routing/repository/frontend/src/github.ts' in sent
    assert not any('node_modules' in name for name in sent)
    # The part's contract is checked against its code and kept beside the routing.
    build, run = runs
    assert {'Dockerfile', 'check.mjs', 'check.sh', 'components/runtime.json',
            'components/src/github.d.ts', 'repository/frontend/src/github.ts',
            'repository/frontend/package-lock.json'} <= set(build['files'])
    assert 'PROJECT=frontend' in build['build']
    assert '--network=none' in run['run'] and run['run'][-1] == 'src/github'
    kept = frontend_session / 'routing/parts/frontend/components'
    assert (kept / 'src/github.d.ts').read_text() == GITHUB_STUB
    # The criterion is routed to the part's seam, and the routing names the part, which
    # the stages after it read from its routes.
    assert result['routes']['FR-3'] == {
        'route': 'component', 'reason': 'Read the posted manifest.',
        'seam': {'part': 'frontend', 'module': 'src/github', 'symbols': ['createGitHubApp']}}
    assert result['parts'] == [{'name': 'frontend', 'root': 'frontend',
                                'language': 'typescript'}]
    assert 'components' not in result
    assert json.loads((frontend_session / 'routing/routes.json').read_text()) == result
    assert judge_routes({key: {**value, 'source': 'routing'}
                         for key, value in result['routes'].items()})[0] == 'routed'


async def test_a_seam_a_part_adds_first_is_kept_unchecked(frontend_session, monkeypatch):
    runs: list = []
    routes = json.loads(json.dumps(PART_ROUTES))
    routes['routes'][1]['seam'] = {'part': 'frontend', 'module': 'src/Flows',
                                   'symbols': ['sessionAttention'],
                                   'added': ['sessionAttention']}
    stub = ('export declare function sessionAttention(flow: unknown, body: unknown): '
            'string[];\n')
    fake_part_docker(monkeypatch, {
        'parts/frontend/components/runtime.json': json.dumps({'project': 'frontend'}),
        'parts/frontend/components/src/Flows.d.ts': stub}, runs, {}, routes=routes)
    result = await ActivityEnvironment().run(activities.route_requirements, 'session',
                                             PYTHON_CONTEXT)
    assert runs == []
    assert (frontend_session / 'routing/parts/frontend/components/src/Flows.d.ts'
            ).read_text() == stub
    assert result['routes']['FR-3']['route'] == 'component'
    # The seam is added first, named after its part.
    assert added_seams(result['routes']) == {'frontend/src/Flows': ['sessionAttention']}


@pytest.mark.parametrize('seam, components, error', [
    ({**PART_SEAM, 'part': 'backend'}, PART_CONTRACT,
     'names a part the repository does not have: backend; its other parts are frontend'),
    (PART_SEAM, {**PART_CONTRACT, 'parts/frontend/components/runtime.json': json.dumps(
        {'project': '.'})}, 'part frontend is unusable: The component contract\'s '
                            'runtime.json is unusable: project . is not a TypeScript project'),
    (PART_SEAM, {**PART_CONTRACT, 'parts/frontend/components/src/github.d.ts':
                 'export declare function createGitHubApp(): void { }\n'},
     'part frontend is unusable: The stub src/github.d.ts is not a stub'),
    ({**PART_SEAM, 'symbols': ['takeAppReturn']}, PART_CONTRACT,
     'names symbols its stub does not define: takeAppReturn'),
    # A seam of the part with its stubs where the service's belong.
    (PART_SEAM, {'components/runtime.json': json.dumps({'project': 'frontend'}),
                 'components/src/github.d.ts': GITHUB_STUB},
     'part frontend is unusable: The component contract has no runtime.json'),
])
async def test_a_part_seam_needs_a_checked_contract_of_that_part(
        frontend_session, monkeypatch, seam, components, error):
    routes = json.loads(json.dumps(PART_ROUTES))
    routes['routes'][1]['seam'] = seam
    fake_part_docker(monkeypatch, components, [], {}, routes=routes)
    with pytest.raises(ApplicationError, match=re.escape(error)):
        await ActivityEnvironment().run(activities.route_requirements, 'session',
                                        PYTHON_CONTEXT)
    assert not (frontend_session / 'routing/parts').exists()


async def test_a_repository_without_other_parts_is_routed_as_before(python_session, monkeypatch):
    sent: dict = {}
    fake_part_docker(monkeypatch, PART_CONTRACT, [], sent)
    with pytest.raises(ApplicationError, match='names a part the repository does not have: '
                                               'frontend; its other parts are none'):
        await ActivityEnvironment().run(activities.route_requirements, 'session',
                                        PYTHON_CONTEXT)
    assert 'parts' not in json.loads(sent['input.json'])
    assert 'part_components' not in json.loads(sent['language.json'])


def test_the_router_is_told_of_the_other_parts_only_when_the_repository_has_them(tmp_path):
    from app.routing import agent
    service = {'label': 'Python (FastAPI)', 'components': 'Component contract (Python).'}
    assert '"part"' not in agent.prompt(service)
    assert 'other parts beside the service' not in agent.prompt(service)
    with_parts = agent.prompt({**service, 'part_components': {
        'typescript': 'Component contract (TypeScript) of a part.'}})
    assert 'other parts beside the service' in with_parts
    assert 'Component contract (Python).' in with_parts
    assert 'Component contract (TypeScript) of a part.' in with_parts
    assert '"seam": {"part": "…", "module"' in with_parts
    # A part's seam needs stubs of that part.
    (tmp_path / 'routes.json').write_text(json.dumps(PART_ROUTES))
    (tmp_path / 'components').mkdir()
    (tmp_path / 'components/app.pyi').write_text('x: int\n')
    with pytest.raises(RuntimeError, match='parts/frontend/components/ holds no stub'):
        agent.validate_completion(tmp_path)
    (tmp_path / 'parts/frontend/components/src').mkdir(parents=True)
    (tmp_path / 'parts/frontend/components/src/github.d.ts').write_text(GITHUB_STUB)
    agent.validate_completion(tmp_path)
