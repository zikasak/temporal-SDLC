"""Verification of a change with component tests: the conformance check of the changed
code against its component contract, the runner built with that code, and the run of the
component tests, in-process and without a network."""
import dataclasses
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.delivery.models import (
    component_run, judge_baseline, reproduce_request, unproven_green, unreproducing_tests,
)
from app.languages import get_language
from app.verification import activities

RUNNER_IMAGE = 'sha256:' + '9' * 64
CHANGED_IMAGE = 'sha256:' + 'e' * 64
CONFORMANCE_IMAGE = 'sha256:' + 'c' * 64
STUB = '''class Rooms:
    def close(self, room: str, merged: bool) -> None:
        """Closes the room, whether its pull request was merged or not."""
        ...
'''
TESTS = '''from service.rooms import Rooms


def test_a_new_room_is_open():
    assert Rooms().state("a") == "open"


def test_a_merged_room_is_closed():
    Rooms().close("a", merged=True)
'''
ADDED = 'tests.test_rooms#test_a_merged_room_is_closed'
EXISTING = 'tests.test_rooms#test_a_new_room_is_open'


def report(*cases: tuple[str, str | None]) -> bytes:
    """A pytest JUnit report of the suite: each test name with its outcome tag, if any."""
    body = ''.join(
        f'<testcase classname="tests.test_rooms" name="{name}">'
        + (f'<{outcome} message="assert False"/>' if outcome else '') + '</testcase>'
        for name, outcome in cases)
    data = f'<testsuites><testsuite>{body}</testsuite></testsuites>'.encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        info = tarfile.TarInfo('./junit.xml')
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return stream.getvalue()


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A delivery whose component tests were written and whose change was implemented."""
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    session_dir = tmp_path / 'session'
    for path, text in {
        'implementation/service/Dockerfile': 'FROM python:3.12-slim',
        'implementation/service/requirements.txt': 'temporalio\n',
        'implementation/service/src/service/rooms.py': 'CHANGED SERVICE SOURCE',
        'baseline/service/src/service/rooms.py': 'BASELINE SERVICE SOURCE',
        'baseline/service/requirements.txt': 'temporalio\n',
        'component-tests/suite/tests/test_rooms.py': TESTS,
        'component-tests/suite/pytest.ini': '[pytest]\n',
        'component-tests/suite/requirements.txt': 'pytest\n',
        'component-tests/image.json': json.dumps({'image_id': RUNNER_IMAGE}),
        'routing/components/runtime.json': json.dumps(
            {'source_root': 'src', 'requirements': ['requirements.txt']}),
        'routing/components/service/rooms.pyi': STUB,
    }.items():
        (session_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (session_dir / path).write_text(text)
    return session_dir


@pytest.fixture
def docker(tmp_path, monkeypatch):
    """Fake Docker control-plane responses; reports stream as a real tar through docker cp."""
    archive = tmp_path / 'archive.tar'
    archive.write_bytes(report(('test_a_new_room_is_open', None),
                               ('test_a_merged_room_is_closed', 'failure')))
    executable = tmp_path / 'bin' / 'docker'
    executable.parent.mkdir()
    executable.write_text(f'#!{sys.executable}\nimport pathlib, sys\n'
                          f'sys.stdout.buffer.write(pathlib.Path({str(archive)!r}).read_bytes())\n')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(executable.parent) + os.pathsep + os.environ['PATH'])
    state = {'calls': [], 'builds': [], 'archive': archive, 'exit_code': 1, 'user': '10001',
             'conformance': None, 'runner_build': None}

    async def fake(*args, **kwargs):
        state['calls'].append(args)
        if args[0] == 'build':
            context = Path(args[-1])
            state['builds'].append({'args': args, 'files': {
                item.relative_to(context).as_posix(): item.read_text()
                for item in context.rglob('*') if item.is_file()}})
            conformance = (context / 'check.sh').is_file()
            failure = state['conformance' if conformance else 'runner_build']
            if failure:
                raise RuntimeError(failure)
            Path(args[args.index('--iidfile') + 1]).write_text(
                CONFORMANCE_IMAGE if conformance else CHANGED_IMAGE)
        if args[:2] == ('image', 'inspect'):
            return state['user']
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': state['exit_code'], 'OOMKilled': False})
        if args[0] == 'logs':
            return 'runner log'
        return ''

    monkeypatch.setattr(docker_agents, 'docker', fake)
    return state


async def test_the_changed_code_is_checked_against_its_contract_then_built_with_the_tests(
        session, docker):
    result = await ActivityEnvironment().run(activities.build_component_runner, 'session',
                                             'python')
    assert result == {'image_id': CHANGED_IMAGE, 'session_id': 'session'}
    conformance, runner = docker['builds']
    # Both are built with the changed code, not the code the delivery started from.
    for build in (conformance, runner):
        assert build['files']['repository/src/service/rooms.py'] == 'CHANGED SERVICE SOURCE'
        assert 'SOURCE_ROOT=src' in build['args']
    assert conformance['files']['components/service/rooms.pyi'] == STUB
    run, = [call for call in docker['calls'] if call[0] == 'run']
    assert '--network=none' in run and run[-1] == 'service.rooms'
    assert runner['files']['suite/tests/test_rooms.py'] == TESTS
    assert runner['files']['Dockerfile'].startswith('# Trusted runner of the component tests')
    manifest = json.loads((session / 'component-verification/image.json').read_text())
    assert manifest['image_id'] == CHANGED_IMAGE


@pytest.mark.parametrize('failing, error', [
    ('conformance', 'The code the component contract describes could not be installed'),
    ('runner_build', 'The component tests could not be built with the changed code: '
                     'ImportError'),
])
async def test_a_change_that_breaks_its_contract_gets_no_runner(session, docker, failing, error):
    docker[failing] = 'ImportError: cannot import name Rooms'
    result = await ActivityEnvironment().run(activities.build_component_runner, 'session',
                                             'python')
    assert result['conformance_error'].startswith(error) and 'image_id' not in result
    assert len(docker['builds']) == (1 if failing == 'conformance' else 2)
    assert not (session / 'component-verification/image.json').exists()


async def test_a_conformance_check_that_finds_a_difference_names_it(session, docker, monkeypatch):
    original = docker_agents.docker

    async def stubtest(*args, **kwargs):
        if args[0] == 'run':
            raise RuntimeError('Docker run failed: error: service.rooms.Rooms.close is '
                               'inconsistent, stub argument "merged" differs')
        return await original(*args, **kwargs)

    monkeypatch.setattr(docker_agents, 'docker', stubtest)
    result = await ActivityEnvironment().run(activities.build_component_runner, 'session',
                                             'python')
    assert result['conformance_error'] == (
        'The component contract does not match the code: error: service.rooms.Rooms.close '
        'is inconsistent, stub argument "merged" differs')


async def test_component_tests_run_without_a_network_and_report_each_tracked_test(
        session, docker):
    result = await ActivityEnvironment().run(activities.run_component_suite, 'session',
                                             RUNNER_IMAGE, 'python', [ADDED])
    assert result['status'] == 'failed' and result['error'] is None
    assert (result['tests'], result['passed'], result['failures']) == (2, 1, 1)
    assert result['tracked'] == {ADDED: {'tests': 1, 'failed': 1}}
    assert result['missing_tests'] == [] and result['runner_log'] == 'runner log'
    assert result['test_failures'][0]['name'] == 'test_a_merged_room_is_closed'
    create, = [call for call in docker['calls'] if call[0] == 'create']
    assert '--network=none' in create and create[-1] == RUNNER_IMAGE
    assert not any(arg in {'-v', '--mount', '--privileged', '-p'} for arg in create)
    name = create[create.index('--name') + 1]
    assert ('rm', '-f', name) in docker['calls']
    assert json.loads((Path(result['path']) / 'result.json').read_text()) == result


async def test_component_tests_run_with_the_runner_built_with_the_change(session, docker):
    (session / 'component-verification').mkdir()
    (session / 'component-verification/image.json').write_text(
        json.dumps({'image_id': CHANGED_IMAGE}))
    docker['archive'].write_bytes(report(('test_a_new_room_is_open', None),
                                         ('test_a_merged_room_is_closed', None)))
    docker['exit_code'] = 0
    result = await ActivityEnvironment().run(activities.run_component_suite, 'session',
                                             CHANGED_IMAGE, 'python', [ADDED])
    assert result['status'] == 'passed' and result['tracked'] == {ADDED: {'tests': 1, 'failed': 0}}


async def test_a_component_test_left_out_of_the_reports_is_missing(session, docker):
    docker['archive'].write_bytes(report(('test_a_merged_room_is_closed', 'failure')))
    result = await ActivityEnvironment().run(activities.run_component_suite, 'session',
                                             RUNNER_IMAGE, 'python', [ADDED])
    assert result['missing_tests'] == [EXISTING]


@pytest.mark.parametrize('change, error', [
    ({'user': 'root'}, 'non-root runtime user'),
    ({'exit_code': 137}, 'timed out or were terminated'),
    ({'exit_code': 0}, 'Runner exit status does not match the test reports'),
])
async def test_a_component_run_that_proves_nothing_is_an_error(session, docker, change, error):
    docker.update(change)
    result = await ActivityEnvironment().run(activities.run_component_suite, 'session',
                                             RUNNER_IMAGE, 'python', [ADDED])
    assert result['status'] == 'error' and error in result['error']


@pytest.mark.parametrize('image, language, error', [
    ('sha256:' + '7' * 64, 'python', 'sources of the component test image are unavailable'),
    ('latest', 'python', 'Invalid component test image ID'),
    (RUNNER_IMAGE, 'plain', 'has no component tests'),
])
async def test_only_a_runner_built_from_the_suite_is_run(session, docker, monkeypatch, image,
                                                         language, error):
    # A language whose criteria cannot be routed to a seam.
    plain = dataclasses.replace(get_language('java'), name='plain', component_contract=None,
                                component_suite=None)
    monkeypatch.setattr(activities, 'session_language', lambda name: (
        plain if name == 'plain' else get_language(name)))
    with pytest.raises(ApplicationError, match=error):
        await ActivityEnvironment().run(activities.run_component_suite, 'session', image,
                                        language, [ADDED])
    assert not [call for call in docker['calls'] if call[0] == 'create']


def result(status: str, tracked: dict, **counts) -> dict:
    return {'status': status, 'tracked': tracked, 'missing_tests': [], 'skipped': 0,
            'failures': 0, 'errors': 0, 'test_skips': [], **counts}


def test_the_baseline_of_a_change_with_component_tests_is_judged_on_both_suites():
    red = result('failed', {ADDED: {'tests': 1, 'failed': 1}}, failures=1)
    green = result('passed', {ADDED: {'tests': 1, 'failed': 0}})
    contract = result('passed', {})
    # Its contract tests are the repository's own, which have to pass as they are.
    assert judge_baseline(contract, [], [], component_run(red, [ADDED])) == ('reproduced', None)
    # A change whose repository has no contract tests is judged on its component tests.
    assert judge_baseline(None, [], [], component_run(red, [ADDED])) == ('reproduced', None)
    status, error = judge_baseline(None, [], [], component_run(green, [ADDED]))
    assert status == 'not_reproduced' and f'{ADDED} (1 of 1)' in error
    status, error = judge_baseline(result('failed', {}, failures=1), [], [],
                                   component_run(red, [ADDED]))
    assert status == 'error' and error.startswith('Contract tests: Existing tests failing')
    status, error = judge_baseline(contract, [], [], component_run(
        result('failed', {ADDED: {'tests': 1, 'failed': 1}}, failures=1, skipped=1), [ADDED]))
    assert status == 'error' and error.startswith('Component tests: Tests skipped themselves')
    assert 'the component tests cannot observe' in error


def test_a_baseline_against_the_code_with_an_added_seam_says_so():
    red = result('failed', {ADDED: {'tests': 1, 'failed': 1}}, failures=1)
    green = result('passed', {ADDED: {'tests': 1, 'failed': 0}})
    contract = result('passed', {})
    assert judge_baseline(contract, [], [], component_run(red, [ADDED]), seam=True) == (
        'reproduced', None)
    # An existing test the seam broke, or an added test it already makes pass, is said of
    # the code with the seam, which retrying the tests adds again.
    status, error = judge_baseline(result('failed', {}, failures=1), [], [],
                                   component_run(red, [ADDED]), seam=True)
    assert status == 'error' and error.startswith(
        'Contract tests: Existing tests failing against the code with only the seam the '
        'change added: 1.')
    assert 'retrying the tests adds it again' in error
    status, error = judge_baseline(contract, [], [], component_run(green, [ADDED]), seam=True)
    assert status == 'not_reproduced' and 'against the code with only the seam' in error
    assert 'adding the seam already changed what the tests observe' in error
    # Without a seam nothing of it is said.
    status, error = judge_baseline(contract, [], [], component_run(green, [ADDED]))
    assert 'seam' not in error and 'as the repository holds it' in error


def test_only_added_tests_alone_to_blame_for_a_baseline_are_sent_back_to_be_rewritten():
    red = result('failed', {ADDED: {'tests': 1, 'failed': 1}}, failures=1)
    green = result('passed', {ADDED: {'tests': 1, 'failed': 0}})
    # Nothing is wrong with added tests that fail without the change.
    assert unreproducing_tests(red, [ADDED]) == {}
    assert unreproducing_tests(green, [ADDED]) == {'passing': [ADDED]}
    # One of a parameterized test's cases passing is enough for it not to reproduce.
    assert unreproducing_tests(result('failed', {ADDED: {'tests': 2, 'failed': 1}},
                                      failures=1), [ADDED]) == {'passing': [ADDED]}
    # An added test that did not run, whether reported missing or not, is the author's.
    assert unreproducing_tests({**red, 'missing_tests': [ADDED]}, [ADDED]) == {
        'missing': [ADDED]}
    assert unreproducing_tests(result('passed', {ADDED: {'tests': 0, 'failed': 0}}),
                               [ADDED]) == {'missing': [ADDED]}
    # Anything beyond the added tests is not corrected by rewriting them.
    for run in (result('error', None), {**green, 'skipped': 1},
                {**green, 'failures': 1, 'status': 'failed'},
                {**green, 'missing_tests': [EXISTING]},
                {**green, 'status': 'failed', 'failures': 1, 'test_failures': [
                    {'class_name': 'tests.test_rooms', 'name': 'x', 'kind': 'failure',
                     'message': 'Failed: Timeout >30.0s'}]}):
        assert unreproducing_tests(run, [ADDED]) is None
    # A corrected repository test may fail there.
    assert unreproducing_tests(result('failed', {ADDED: {'tests': 1, 'failed': 0},
                                                 EXISTING: {'tests': 1, 'failed': 1}},
                                      failures=1), [ADDED], [EXISTING]) == {'passing': [ADDED]}
    request = reproduce_request({'passing': [ADDED], 'missing': ['tests.other#test_x']}, 1)
    assert request['reproduce'] is True and request['attempt'] == 1
    assert request['broken_tests'] == [
        {'class_name': 'tests.test_rooms', 'name': 'test_a_merged_room_is_closed'},
        {'class_name': 'tests.other', 'name': 'test_x'}]


def test_a_green_run_of_either_suite_with_a_test_that_did_not_run_proves_nothing():
    passed = result('passed', {})
    assert unproven_green(passed, passed) is None and unproven_green(None, passed) is None
    assert unproven_green(passed, {**passed, 'skipped': 1}).startswith(
        'Component tests: Tests skipped in the run against the changed service: 1')
    assert unproven_green({**passed, 'missing_tests': ['A#b']}, passed).startswith(
        'Tests of the suite that did not run')
