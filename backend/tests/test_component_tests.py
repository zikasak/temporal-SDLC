"""The author of a change's component tests, which never sees the code, and the trusted
runner that builds its suite with the code."""
import dataclasses
import json
import tarfile
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.api_design.models import empty_api
from app.languages import get_language
from app.test_generation import activities as contract_tests
from app.test_generation import agent
from app.test_generation import components
from app.verification import activities as verification

SUITE = get_language('python').component_suite
REQUIREMENT = {'category': 'functional', 'title': 'T', 'description': 'D', 'priority': 'must'}
BRIEF = {'requirements': [{**REQUIREMENT, 'id': 'FR-2'}, {**REQUIREMENT, 'id': 'FR-3'}],
         'requirements_document': '# Brief', 'retired': []}
SEAM = {'module': 'service.rooms', 'symbols': ['Rooms.close', 'Rooms.state']}
ROUTES = {'FR-3': {'route': 'component', 'reason': 'Close, then read the state.',
                   'seam': SEAM}}
CONTEXT = {'requirements': [{**REQUIREMENT, 'id': 'FR-1'}],
           'requirements_document': '# Contract', 'language': 'python',
           'api_design': empty_api(), 'brief': BRIEF, 'routes': ROUTES,
           'repository': {'full_name': 'octo/hotel'}}
STUB = '''class Rooms:
    def close(self, room: str, merged: bool) -> None:
        """Closes the room, whether its pull request was merged or not."""
        ...

    def state(self, room: str) -> str: ...
'''
RUNTIME = {'source_root': 'src', 'requirements': ['requirements.txt']}
EXISTING = '''from service.rooms import Rooms


def test_a_new_room_is_open():
    assert Rooms().state("a") == "open"
'''
ADDED = '''from service.rooms import Rooms


def test_a_merged_room_is_closed():
    rooms = Rooms()
    rooms.close("a", merged=True)
    assert rooms.state("a") == "closed"
'''


@pytest.fixture
async def session(tmp_path, monkeypatch):
    """A Python repository with component tests, routed and recorded as the delivery
    leaves it before its test authors run."""
    for module in (components, verification, contract_tests):
        monkeypatch.setattr(module, 'GENERATED_CODE_DIR', tmp_path)
    session_dir = tmp_path / 'session'
    for path, text in {
        'repository/Dockerfile': 'FROM python:3.12-slim',
        'repository/requirements.txt': 'temporalio\n',
        'repository/src/service/rooms.py': 'PRIVATE SERVICE SOURCE',
        'repository/contract-tests/tests/test_rooms.py': 'def test_http(): pass\n',
        'repository/contract-tests/pytest.ini': '[pytest]\n',
        'repository/contract-tests/requirements.txt': 'pytest\n',
        'repository/component-tests/tests/test_rooms.py': EXISTING,
        'repository/component-tests/pytest.ini': '[pytest]\n',
        'repository/component-tests/requirements.txt': 'pytest\n',
        'repository/component-tests/Dockerfile': 'FROM untrusted',
        'routing/components/runtime.json': json.dumps(RUNTIME),
        'routing/components/service/rooms.pyi': STUB,
    }.items():
        (session_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (session_dir / path).write_text(text)
    recorded = await ActivityEnvironment().run(verification.snapshot_repository_tests,
                                               'session', 'python')
    assert recorded == {'tests': 1, 'component_tests': 1}
    return session_dir


def fake_docker(monkeypatch, written: dict[str, str], sent: dict, builds: list,
                build_fails: bool = False):
    """Stands in for the Docker CLI and the agent's filesystem; `written` is what the agent
    leaves in /workspace/tests."""
    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/tests/.'):
            output = Path(args[2])
            for path, text in written.items():
                (output / path).parent.mkdir(parents=True, exist_ok=True)
                (output / path).write_text(text)
        if args[0] == 'build' and '--iidfile' in args:
            context = Path(args[-1])
            builds.append({'args': args, 'files': {
                item.relative_to(context).as_posix(): item.read_text()
                for item in context.rglob('*') if item.is_file()}})
            if build_fails:
                raise RuntimeError('ImportError while importing test module')
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'e' * 64)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)


SUITE_FILES = {'tests/test_rooms.py': EXISTING + '\n\n' + ADDED.split('\n\n\n')[1],
               'pytest.ini': '[pytest]\n', 'requirements.txt': 'pytest\n',
               'Dockerfile': 'FROM untrusted-agent-image', 'run-tests.sh': 'exit 0'}


async def test_the_author_sees_the_stubs_not_the_code_and_the_runner_builds_them_together(
        session, monkeypatch):
    sent: dict = {}
    builds: list = []
    fake_docker(monkeypatch, SUITE_FILES, sent, builds)
    result = await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                             CONTEXT)
    # The author's workspace: the repository's component tests, the template, the stubs.
    assert sent['tests/tests/test_rooms.py'] == EXISTING
    assert sent['contracts/components/service/rooms.pyi'] == STUB
    assert json.loads(sent['contracts/components/runtime.json']) == RUNTIME
    assert 'tests/Dockerfile' not in sent and 'tests/prepare.py' not in sent
    assert not any('PRIVATE' in text or 'test_http' in text for text in sent.values())
    assert 'openapi.yaml' not in sent
    payload = json.loads(sent['input.json'])
    assert payload['component'] is True and payload['brief'] == BRIEF
    assert payload['routes'] == ROUTES and 'api_design' not in payload
    assert json.loads(sent['language.json'])['instructions'] == SUITE.instructions
    # The runner: the template's trusted files, the code as the delivery started from it
    # (never its tests), the sanitized suite, and how to install the code.
    build, = builds
    files = build['files']
    for name in SUITE.runner:
        assert files[name] == (SUITE.template / name).read_text()
    assert files['repository/src/service/rooms.py'] == 'PRIVATE SERVICE SOURCE'
    assert not any(name.startswith(('repository/contract-tests', 'repository/component-tests'))
                   for name in files)
    assert files['suite/tests/test_rooms.py'] == SUITE_FILES['tests/test_rooms.py']
    assert 'suite/Dockerfile' not in files and 'suite/run-tests.sh' not in files
    assert 'SOURCE_ROOT=src' in build['args'] and 'REQUIREMENTS=requirements.txt' in build['args']
    published = session / 'component-tests/suite'
    assert result['path'] == str(published) and result['image_id'] == 'sha256:' + 'e' * 64
    assert result['added_tests'] == ['tests.test_rooms#test_a_merged_room_is_closed']
    assert result['changed_tests'] == [] and result['removed_tests'] == []
    assert not (published / 'Dockerfile').exists()


async def test_a_change_that_added_its_seam_builds_the_runner_with_the_code_with_it(
        session, monkeypatch):
    prepared = session / 'seam/service/src/service/rooms.py'
    prepared.parent.mkdir(parents=True)
    prepared.write_text('PRIVATE SERVICE SOURCE WITH THE SEAM')
    (session / 'seam/service/requirements.txt').write_text('temporalio\n')
    sent: dict = {}
    builds: list = []
    fake_docker(monkeypatch, SUITE_FILES, sent, builds)
    await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                    {**CONTEXT, 'seam_prepared': True})
    build, = builds
    assert build['files']['repository/src/service/rooms.py'] == (
        'PRIVATE SERVICE SOURCE WITH THE SEAM')
    # The author still sees no code, with the seam or without it.
    assert not any('PRIVATE' in text for text in sent.values())
    assert 'seam_prepared' not in json.loads(sent['input.json'])

    # Without the code with the seam no runner can show anything through it.
    import shutil
    shutil.rmtree(session / 'seam')
    with pytest.raises(ApplicationError, match='code with the seam'):
        await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                        {**CONTEXT, 'seam_prepared': True})


@pytest.mark.parametrize('written, error', [
    ({**SUITE_FILES, 'tests/test_rooms.py': EXISTING.replace('"open"', '"closed"') + ADDED},
     'changed or removed tests it may not touch'),
    ({**SUITE_FILES, 'tests/test_rooms.py': EXISTING}, 'did not add a regression test'),
    ({'tests/test_rooms.py': ADDED}, 'did not produce a test suite'),
])
async def test_the_repositorys_component_tests_are_frozen(session, monkeypatch, written, error):
    fake_docker(monkeypatch, written, {}, [])
    with pytest.raises(RuntimeError, match=error):
        await ActivityEnvironment().run(components.generate_component_tests, 'session', CONTEXT)
    assert not (session / 'component-tests/suite').exists()


async def test_a_suite_that_does_not_build_with_the_code_is_not_published(session, monkeypatch):
    fake_docker(monkeypatch, SUITE_FILES, {}, [], build_fails=True)
    with pytest.raises(RuntimeError, match='ImportError'):
        await ActivityEnvironment().run(components.generate_component_tests, 'session', CONTEXT)
    assert not (session / 'component-tests/suite').exists()


async def test_the_author_may_name_only_its_own_criteria_unobservable(session, monkeypatch):
    def declined(requirement: str) -> dict[str, str]:
        return {'unobservable.json': json.dumps({'unobservable': [
            {'requirement_id': requirement, 'reason': 'Happens in no stubbed symbol.'}]})}

    fake_docker(monkeypatch, declined('FR-3'), {}, [])
    assert await ActivityEnvironment().run(
        components.generate_component_tests, 'session', CONTEXT) == {
        'session_id': 'session', 'unobservable': [
            {'requirement_id': 'FR-3', 'reason': 'Happens in no stubbed symbol.'}]}
    # FR-2 is the contract tests' to prove.
    fake_docker(monkeypatch, declined('FR-2'), {}, [])
    with pytest.raises(RuntimeError, match='a requirement the brief does not have: FR-2'):
        await ActivityEnvironment().run(components.generate_component_tests, 'session', CONTEXT)


@pytest.mark.parametrize('change, error', [
    ({'language': 'plain'}, 'has no component tests'),
    ({'brief': None}, "only for a change's criteria"),
    ({'routes': {'FR-2': {'route': 'contract', 'reason': 'GET.'}}}, 'No criterion is routed'),
    ({'routes': {'FR-3': {'route': 'component', 'reason': 'R.'}}}, 'names no seam'),
])
async def test_component_tests_need_a_change_routed_to_a_seam(session, monkeypatch, change, error):
    fake_docker(monkeypatch, SUITE_FILES, {}, [])
    # A language whose criteria cannot be routed to a seam.
    plain = dataclasses.replace(get_language('java'), name='plain', component_contract=None,
                                component_suite=None)
    monkeypatch.setattr(components, 'session_language', lambda name: (
        plain if name == 'plain' else get_language(name)))
    with pytest.raises(ApplicationError, match=error):
        await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                        {**CONTEXT, **change})


async def test_component_tests_need_the_routings_contract(session, monkeypatch):
    (session / 'routing/components/runtime.json').unlink()
    fake_docker(monkeypatch, SUITE_FILES, {}, [])
    with pytest.raises(ApplicationError, match='component contract of the routing is unavailable'):
        await ActivityEnvironment().run(components.generate_component_tests, 'session', CONTEXT)


async def test_the_contract_author_is_given_its_criteria_and_may_decline_only_those(
        session, monkeypatch):
    sent: dict = {}
    context = {**CONTEXT, 'routes': {'FR-2': {'route': 'contract', 'reason': 'GET /rooms.'}}}
    fake_docker(monkeypatch, {'unobservable.json': json.dumps({'unobservable': [
        {'requirement_id': 'FR-3', 'reason': 'R.'}]})}, sent, [])
    with pytest.raises(RuntimeError, match='a requirement the brief does not have: FR-3'):
        await ActivityEnvironment().run(contract_tests.generate_service_tests, 'session', context)
    assert json.loads(sent['input.json'])['routes'] == context['routes']


BROKEN = {'class_name': 'tests.test_rooms', 'name': 'test_a_merged_room_is_closed'}
SECOND = '''

def test_an_unmerged_room_is_closed():
    rooms = Rooms()
    rooms.close("a", merged=False)
    assert rooms.state("a") == "closed"
'''


async def published(session, monkeypatch, files=None):
    """The suite as its first run publishes it: the repository's test and two added ones."""
    fake_docker(monkeypatch, files or {**SUITE_FILES, 'tests/test_rooms.py':
                                       SUITE_FILES['tests/test_rooms.py'] + SECOND}, {}, [])
    return await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                           CONTEXT)


@pytest.mark.parametrize('repair', ['diagnosis', 'review'])
async def test_a_repair_continues_the_published_suite_and_changes_only_what_it_may(
        session, monkeypatch, repair):
    first = await published(session, monkeypatch)
    suite = (session / 'component-tests/suite/tests/test_rooms.py').read_text()
    context = {**CONTEXT, 'repair': {
        'attempt': 1, 'instructions': 'Close it.', 'broken_tests': [BROKEN],
        'test_failures': []} if repair == 'diagnosis' else {
        'attempt': 1, 'review': {'findings': [{'summary': 'Weak.', 'reason': 'R.'}]}}}
    # The diagnosis found one added test broken: the other added one stays as published.
    # A review correction may change the delivery's own tests, never the repository's.
    corrected = suite.replace('rooms.close("a", merged=True)', 'rooms.close("a", True)')
    touched = suite.replace('merged=False', 'False')
    sent: dict = {}
    fake_docker(monkeypatch, {**SUITE_FILES, 'tests/test_rooms.py': corrected}, sent, [])
    result = await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                             context)
    # It starts from its own published suite, with the stubs and the repair.
    assert sent['tests/tests/test_rooms.py'] == suite
    assert sent['contracts/components/service/rooms.pyi'] == STUB
    payload = json.loads(sent['input.json'])
    assert payload['repair'] == context['repair'] and payload['component'] is True
    assert result['added_tests'] == first['added_tests']
    assert (session / 'component-tests/suite/tests/test_rooms.py').read_text() == corrected
    # No regression test needs adding, and none may be declined.
    fake_docker(monkeypatch, {**SUITE_FILES, 'tests/test_rooms.py': EXISTING}, {}, [])
    if repair == 'diagnosis':
        with pytest.raises(RuntimeError, match='changed or removed tests it may not touch'):
            await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                            context)
        fake_docker(monkeypatch, {**SUITE_FILES, 'tests/test_rooms.py': touched}, {}, [])
        with pytest.raises(RuntimeError, match='test_an_unmerged_room_is_closed'):
            await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                            context)
    else:
        assert (await ActivityEnvironment().run(
            components.generate_component_tests, 'session', context))['added_tests'] == []
        fake_docker(monkeypatch, {**SUITE_FILES, 'tests/test_rooms.py':
                                  EXISTING.replace('"open"', '"closed"')}, {}, [])
        with pytest.raises(RuntimeError, match='test_a_new_room_is_open'):
            await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                            context)
    fake_docker(monkeypatch, {'unobservable.json': json.dumps({'unobservable': [
        {'requirement_id': 'FR-3', 'reason': 'R.'}]}), **SUITE_FILES,
        'tests/test_rooms.py': corrected}, {}, [])
    assert 'unobservable' not in await ActivityEnvironment().run(
        components.generate_component_tests, 'session', context)


async def test_a_repair_needs_the_published_suite(session, monkeypatch):
    fake_docker(monkeypatch, SUITE_FILES, {}, [])
    with pytest.raises(ApplicationError, match='Generated component tests are unavailable'):
        await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                        {**CONTEXT, 'repair': {'attempt': 1, 'broken_tests': []}})


def test_each_author_is_told_its_own_task():
    language = {'instructions': 'INSTRUCTIONS'}
    component = agent.prompt({'component': True, 'brief': BRIEF, 'routes': ROUTES}, language)
    assert component.startswith('Add regression tests to the component test suite')
    assert 'INSTRUCTIONS' in component and agent.UNOBSERVABLE in component
    contract = agent.prompt({'brief': BRIEF, 'routes': {}}, language)
    assert agent.ROUTES_PROMPT not in contract and agent.UNOBSERVABLE_PROMPT in contract
    routed = agent.prompt({'brief': BRIEF, 'routes': {'FR-2': {}}}, language)
    assert agent.ROUTES_PROMPT in routed
    assert agent.UNOBSERVABLE_PROMPT not in agent.prompt(
        {'brief': BRIEF, 'routes': {'FR-2': {}}, 'repair': {'attempt': 1}}, language)
    # The component tests' repairs keep to the stubs; neither may decline a criterion.
    inputs = {'component': True, 'brief': BRIEF, 'routes': ROUTES}
    repaired = agent.prompt({**inputs, 'repair': {'attempt': 1}}, language)
    reviewed = agent.prompt({**inputs, 'repair': {'attempt': 1, 'review': {'findings': []}}},
                            language)
    assert repaired.endswith(agent.COMPONENT_REPAIR_PROMPT)
    assert reviewed.endswith(agent.COMPONENT_REVIEW_PROMPT)
    assert all(agent.UNOBSERVABLE not in text for text in (repaired, reviewed))
    # Added tests the baseline showed not failing without the change are rewritten.
    rewritten = agent.prompt({**inputs, 'repair': {'attempt': 1, 'reproduce': True}}, language)
    assert rewritten.endswith(agent.COMPONENT_REPRODUCE_PROMPT)
    assert agent.REPRODUCE_PROMPT in agent.prompt(
        {'brief': BRIEF, 'routes': {}, 'repair': {'attempt': 1, 'reproduce': True}}, language)
    assert agent.CORRECTED_REPRODUCE_PROMPT not in rewritten
    # After a correction of review findings, a test a finding asked for may be removed.
    corrected = {'attempt': 1, 'reproduce': True, 'findings': [{'summary': 'S', 'reason': 'R'}]}
    assert agent.prompt({**inputs, 'repair': corrected}, language).endswith(
        agent.COMPONENT_REPRODUCE_PROMPT + agent.CORRECTED_REPRODUCE_PROMPT)
    assert agent.CORRECTED_REPRODUCE_PROMPT in agent.prompt(
        {'brief': BRIEF, 'routes': {}, 'repair': corrected}, language)


def test_the_component_suite_template_publishes_no_runner(tmp_path):
    contract_tests.copy_test_suite(SUITE, SUITE.template, tmp_path / 'suite')
    assert sorted(item.name for item in (tmp_path / 'suite').iterdir()) == [
        'README.md', 'pytest.ini', 'requirements.txt']
    assert all((SUITE.template / name).is_file() for name in SUITE.runner)
