import asyncio
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
from app.verification import activities

SERVICE_IMAGE = 'sha256:' + 'a' * 64
TEST_IMAGE = 'sha256:' + 'b' * 64
PASS = b'<testsuite tests="1"><testcase classname="Contract" name="http"/></testsuite>'
FAIL = b'<testsuite><testcase classname="Contract" name="http"><failure message="Expected 200, got 500"/></testcase></testsuite>'
ERROR = b'<testsuite><testcase classname="Contract" name="http"><error message="Connection refused"/></testcase></testsuite>'


def report_archive(data: bytes | None, *, link=False) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        if data is not None:
            info = tarfile.TarInfo('./TEST-Contract.xml')
            if link:
                info.type = tarfile.SYMTYPE
                info.linkname = '/etc/passwd'
            else:
                info.size = len(data)
            tar.addfile(info, None if link else io.BytesIO(data))
    return stream.getvalue()


@pytest.fixture
def transport(tmp_path, monkeypatch):
    """Fake Docker control-plane responses, but stream real tar/XML through docker cp."""
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path / 'generated')
    archive = tmp_path / 'archive.tar'
    archive.write_bytes(report_archive(PASS))
    executable = tmp_path / 'docker'
    executable.write_text(
        f'#!{sys.executable}\nimport pathlib, sys\n'
        f'sys.stdout.buffer.write(pathlib.Path({str(archive)!r}).read_bytes())\n'
    )
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    state = {'exit_code': 0, 'failure': None, 'calls': [], 'created': set(),
             'removed': set(), 'networks': set(), 'archive': archive}

    async def docker(*args, **kwargs):
        state['calls'].append(args)
        if args[:2] == ('image', 'inspect'):
            if state['failure'] == 'missing_image':
                raise RuntimeError('No such image')
            if state['failure'] == 'root_image':
                return 'root'
            if state['failure'] == 'root_uid':
                return '000:10001'
            return '10001:10001'
        if args[:2] == ('network', 'create'):
            state['networks'].add(args[-1])
        if args[:2] == ('network', 'rm'):
            state['networks'].discard(args[-1])
        if args[0] == 'create':
            assert not any(arg in {'-v', '--mount', '--privileged', '--network=host', '-p'} for arg in args)
            assert not any('OPENAI' in arg or 'docker.sock' in arg for arg in args)
            state['created'].add(args[args.index('--name') + 1])
        if args[0] == 'wait':
            if state['failure'] == 'timeout':
                raise TimeoutError('runner deadline exceeded')
            if state['failure'] == 'cancel':
                raise asyncio.CancelledError()
        if args[0] == 'logs':
            return 'bounded container log'
        if args[0] == 'inspect':
            is_runner = args[-1].endswith('-tests')
            return json.dumps({
                'ExitCode': state['exit_code'] if is_runner else 0,
                'Running': not is_runner and state['failure'] != 'service_stopped',
                'OOMKilled': is_runner and state['failure'] == 'oom',
            })
        if args[0] == 'rm':
            state['removed'].add(args[-1])
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    return state


@pytest.mark.parametrize('data,exit_code,status,counts', [
    (PASS, 0, 'passed', (1, 1, 0, 0, 0)),
    (FAIL, 1, 'failed', (1, 0, 1, 0, 0)),
    (ERROR, 1, 'failed', (1, 0, 0, 1, 0)),
    (b'<testsuites tests="99"><testsuite tests="99"><testcase name="ok"/>'
     b'<testcase name="later"><skipped/></testcase></testsuite></testsuites>',
     0, 'passed', (2, 1, 0, 0, 1)),
    (FAIL, 0, 'error', (1, 0, 1, 0, 0)),
    (PASS, 1, 'error', (1, 1, 0, 0, 0)),
])
async def test_verification_publishes_test_outcomes_and_releases_resources(
    transport, data, exit_code, status, counts,
):
    transport['archive'].write_bytes(report_archive(data))
    transport['exit_code'] = exit_code
    result = await ActivityEnvironment().run(activities.verify_service, 'session', SERVICE_IMAGE, TEST_IMAGE, 'java')
    assert result['status'] == status
    assert tuple(result[key] for key in ('tests', 'passed', 'failures', 'errors', 'skipped')) == counts
    if counts[2] or counts[3]:
        assert result['test_failures'][0]['name'] == 'http'
        assert result['test_failures'][0]['message']
    manifest = Path(result['path']).parent / 'result.json'
    assert json.loads(manifest.read_text()) == result
    assert (Path(result['path']) / 'reports/TEST-Contract.xml').read_bytes() == data
    assert result['service_log'] == result['runner_log'] == 'bounded container log'
    assert len(transport['created']) == 2
    assert transport['created'] <= transport['removed']
    assert not transport['networks']
    creates = [call for call in transport['calls'] if call[0] == 'create']
    assert creates[0][-1] == SERVICE_IMAGE and creates[1][-1] == TEST_IMAGE
    assert 'SERVICE_BASE_URL=http://service:8080' in creates[1]
    assert creates[0][creates[0].index('--network') + 1] == creates[1][creates[1].index('--network') + 1]


@pytest.mark.parametrize('failure,exit_code', [
    ('missing_image', 0), ('root_image', 0), ('root_uid', 0), ('timeout', 0), ('service_stopped', 0),
    ('oom', 137), (None, 124), (None, 125), (None, 137), (None, 143), (None, 2),
])
async def test_infrastructure_errors_are_not_assertion_failures(transport, failure, exit_code):
    transport['failure'] = failure
    transport['exit_code'] = exit_code
    result = await ActivityEnvironment().run(activities.verify_service, 'session', SERVICE_IMAGE, TEST_IMAGE, 'java')
    assert result['status'] == 'error' and result['error']
    assert transport['created'] <= transport['removed'] and not transport['networks']
    if failure in {'missing_image', 'root_image', 'root_uid'}:
        assert not transport['created']


@pytest.mark.parametrize('data,link', [
    (None, False), (b'<testsuite/>', False),
    (b'<testsuite><testcase><skipped/></testcase></testsuite>', False),
    (b'not XML', False), (b'<not-a-report/>', False),
    (b'<!DOCTYPE testsuite [<!ENTITY a "boom">]><testsuite><testcase name="&a;"/></testsuite>', False),
    ('<!DOCTYPE testsuite [<!ENTITY a "boom">]><testsuite/>'.encode('utf-16'), False),
    (PASS, True), (b'x' * (1024 * 1024 + 1), False),
    (b'x' * (8 * 1024 * 1024 + 1), False),
])
async def test_missing_or_untrusted_reports_cannot_pass(transport, data, link):
    transport['archive'].write_bytes(report_archive(data, link=link))
    result = await ActivityEnvironment().run(activities.verify_service, 'session', SERVICE_IMAGE, TEST_IMAGE, 'java')
    assert result['status'] == 'error' and result['error']
    assert transport['created'] <= transport['removed'] and not transport['networks']


async def test_failure_details_are_bounded_without_losing_totals(transport):
    data = ('<testsuite>' + '<testcase name="bad"><failure message="wrong"/></testcase>' * 51 + '</testsuite>').encode()
    transport['archive'].write_bytes(report_archive(data))
    transport['exit_code'] = 1
    result = await ActivityEnvironment().run(activities.verify_service, 'session', SERVICE_IMAGE, TEST_IMAGE, 'java')
    assert result['status'] == 'failed' and result['failures'] == 51
    assert len(result['test_failures']) == 50 and result['details_truncated']


async def test_cancellation_cleans_up_without_publishing_a_result(transport, tmp_path):
    transport['failure'] = 'cancel'
    with pytest.raises(asyncio.CancelledError):
        await ActivityEnvironment().run(activities.verify_service, 'session', SERVICE_IMAGE, TEST_IMAGE, 'java')
    assert transport['created'] <= transport['removed'] and not transport['networks']
    assert not (tmp_path / 'generated/session/verification/result.json').exists()


@pytest.mark.parametrize('session,service_image,test_image,language', [
    ('../escape', SERVICE_IMAGE, TEST_IMAGE, 'java'),
    ('session', 'untrusted:latest', TEST_IMAGE, 'java'),
    ('session', SERVICE_IMAGE, '--privileged', 'java'),
    ('session', SERVICE_IMAGE, TEST_IMAGE, 'cobol'), ('session', SERVICE_IMAGE, TEST_IMAGE, None),
])
async def test_invalid_inputs_never_touch_docker_or_files(
        transport, tmp_path, session, service_image, test_image, language):
    with pytest.raises(ApplicationError):
        await ActivityEnvironment().run(
            activities.verify_service, session, service_image, test_image, language)
    assert not transport['calls'] and not (tmp_path / 'generated').exists()


async def test_named_tests_are_reported_one_by_one(transport, tmp_path):
    # The suite the runner image was built from; its build left `hidden` out of the run.
    suite = tmp_path / 'generated/session/tests'
    (suite / 'suite/src/test/java/com/example').mkdir(parents=True)
    (suite / 'suite/src/test/java/com/example/OrdersTest.java').write_text(
        'package com.example;\nclass OrdersTest {\n' + ''.join(
            f'  @Test void {name}() {{}}\n' for name in ('lists', 'refuses', 'accepts', 'hidden'))
        + '}\n')
    (suite / 'image.json').write_text(json.dumps({'image_id': TEST_IMAGE}))
    case = '<testcase classname="com.example.{}" name="{}">{}</testcase>'
    failure = '<failure message="200"/>'
    data = ('<testsuite>' + case.format('OrdersTest', 'lists', failure)
            + case.format('OrdersTest', 'refuses', failure)
            + case.format('OrdersTest$Sizes', 'accepts(int)[1]', '')
            + case.format('OrdersTest$Sizes', 'accepts(int)[2]', '<error message="x"/>')
            + '</testsuite>').encode()
    transport['archive'].write_bytes(report_archive(data))
    transport['exit_code'] = 1
    result = await ActivityEnvironment().run(
        activities.verify_service, 'session', SERVICE_IMAGE, TEST_IMAGE, 'java',
        ['com.example.OrdersTest#refuses', 'com.example.OrdersTest#accepts',
         'com.example.OrdersTest#renamed'])
    assert (result['tests'], result['failures'], result['errors']) == (4, 2, 1)
    # Each test by itself, whatever class or invocation its report entries belong to.
    assert result['tracked'] == {'com.example.OrdersTest#refuses': {'tests': 1, 'failed': 1},
                                 'com.example.OrdersTest#accepts': {'tests': 2, 'failed': 1},
                                 'com.example.OrdersTest#renamed': {'tests': 0, 'failed': 0}}
    assert result['missing_tests'] == ['com.example.OrdersTest#hidden']

    # Sources of another image say nothing about what this one should have run.
    (suite / 'image.json').write_text(json.dumps({'image_id': SERVICE_IMAGE}))
    with pytest.raises(ApplicationError, match='sources of the test image'):
        await ActivityEnvironment().run(
            activities.verify_service, 'session', SERVICE_IMAGE, TEST_IMAGE, 'java', [])


async def test_baseline_image_is_the_repositorys_service_without_its_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    checkout = tmp_path / 'session/repository'
    for name in ('src/main/Orders.java', 'src/test/OrdersTest.java', 'contract-tests/pom.xml',
                 'contracts/api.json', '.git/config'):
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(name)
    built = []

    async def docker(*args, **kwargs):
        context = Path(args[-1])
        built.extend(sorted(str(item.relative_to(context)) for item in context.rglob('*')
                            if item.is_file()))
        Path(args[args.index('--iidfile') + 1]).write_text(SERVICE_IMAGE)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    run = lambda: ActivityEnvironment().run(activities.build_baseline, 'session', 'java')
    with pytest.raises(ApplicationError, match='no Dockerfile'):
        await run()
    (checkout / 'Dockerfile').write_text('FROM scratch')
    assert (await run())['image_id'] == SERVICE_IMAGE
    assert built == ['Dockerfile', 'src/main/Orders.java']
