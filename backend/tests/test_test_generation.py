# Assertions read optional fields directly: a None there fails the test anyway.
# pyright: reportOptionalMemberAccess=false
import asyncio
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.api_design.models import empty_api
from app.api_design.openapi import render_openapi
from app.requirements.document import render_requirements_markdown
from app.requirements.models import Requirement
from app.languages import OTHER_PARTS_BUILD_DIRS, get_language
from app.test_generation import activities
from app.verification import activities as verification

TEMPLATE = get_language('java').test_suite.template


async def record_repository_tests(tmp_path, monkeypatch) -> dict:
    """What the delivery does before any author runs."""
    monkeypatch.setattr(verification, 'GENERATED_CODE_DIR', tmp_path)
    return await ActivityEnvironment().run(verification.snapshot_repository_tests,
                                           'session', 'java')


@pytest.mark.parametrize('failure', [None, 'agent', 'build', 'empty', 'cancel', 'timeout'])
async def test_contract_only_generation_publishes_compiled_suite(tmp_path, monkeypatch, failure):
    """Owns source isolation, Docker lifecycle and publication at the host boundary."""
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    # The saved OpenAI endpoint, http://localhost:1234/v1 with the key test-secret-key,
    # comes from the saved_model_settings fixture.
    implementation = tmp_path / 'session/implementation/service'
    implementation.mkdir(parents=True)
    private_source = implementation / 'Private.java'
    private_source.write_text('PRIVATE IMPLEMENTATION SOURCE')
    server = tmp_path / 'session/server'
    server.mkdir()
    (server / 'Generated.java').write_text('PRIVATE GENERATED SOURCE')
    requirements: list[Requirement] = [
        {'id': 'FR-1', 'category': 'functional', 'title': 'Create orders',
         'description': 'Clients create orders.', 'priority': 'must', 'rationale': 'Core flow'}]
    context = {'requirements': requirements,
               'requirements_document': render_requirements_markdown(
                   requirements, 'Orders are never deleted.'),
               'api_design': empty_api(), 'language': 'java',
               'implementation': private_source.read_text(),
               'source_path': str(implementation)}
    image_id = 'sha256:' + 'c' * 64
    container_name = None
    removed = []
    stopped = []
    runner_built = False

    async def docker(*args, **kwargs):
        nonlocal container_name, runner_built
        if args[0] == 'stop':
            # Stopped with time to export its traces, before it is removed.
            assert not removed
            stopped.append((args[-1], args[args.index('-t') + 1]))
        if args[0] == 'create':
            container_name = args[args.index('--name') + 1]
            assert container_name.startswith('test-generation-')
            assert not any(arg in {'-v', '--volume', '--mount', '--volumes-from', '--privileged',
                                   '--network=host'} for arg in args)
            assert args[args.index('--user') + 1] == '10001:10001'
            assert kwargs['env']['OPENAI_BASE_URL'] == 'http://host.docker.internal:1234/v1'
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                members = archive.getmembers()
                assert all(item.uid == 10001 and not item.issym() for item in members)
                sent = [archive.extractfile(item).read() for item in members if item.isfile()]
                assert not any(b'PRIVATE' in data for data in sent)
                payload = json.loads(archive.extractfile('input.json').read())
                # One copy of each contract: the API only as OpenAPI, requirements only
                # as the structured list plus the narrative its tables do not repeat.
                assert payload == {'requirements': requirements,
                                   'requirements_narrative': 'Orders are never deleted.'}
                assert archive.extractfile('openapi.yaml').read().decode() == (
                    render_openapi(context['api_design']))
                # The stack is the registered language's, not the test author's choice.
                assert json.loads(archive.extractfile('language.json').read()) == {
                    'name': 'java', 'build_dirs': ['target', *OTHER_PARTS_BUILD_DIRS], 'instructions': get_language('java').test_suite.instructions,
                    'test_glob': 'src/test/java/**/*Test.java'}
        if args[0] == 'wait' and failure == 'cancel':
            raise asyncio.CancelledError()
        if args[0] == 'wait' and failure == 'timeout':
            raise TimeoutError()
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 1 if failure == 'agent' else 0})
        if args[0] == 'logs':
            return 'Provider rejected test-secret-key: model unavailable'
        if args[0] == 'cp' and args[1].endswith(':/workspace/tests/.'):
            output = Path(args[2])
            (output / 'pom.xml').write_text((TEMPLATE / 'pom.xml').read_text())
            if failure != 'empty':
                tests = output / 'src/test/java/example'
                tests.mkdir(parents=True)
                (tests / 'ContractTest.java').write_text('test suite from external agent')
                (tests / 'linked.java').symlink_to(private_source)
            (output / 'src/main').mkdir(parents=True)
            (output / 'src/main/Service.java').write_text('unwanted production source')
            (output / 'target').mkdir()
            (output / 'target/build.log').write_text('unwanted build output')
            (output / 'Dockerfile').write_text('FROM untrusted-agent-image')
            (output / 'run-tests.sh').write_text('exit 0 # bypass all tests')
        if args[0] == 'build' and '--iidfile' in args:
            runner_built = True
            build = Path(args[-1])
            assert (build / 'src/test/java/example/ContractTest.java').is_file()
            assert not (build / 'src/main').exists() and not (build / 'target').exists()
            assert not (build / 'src/test/java/example/linked.java').exists()
            assert (build / 'Dockerfile').read_text() == (TEMPLATE / 'Dockerfile').read_text()
            assert (build / 'run-tests.sh').read_text() == (TEMPLATE / 'run-tests.sh').read_text()
            if failure == 'build':
                raise RuntimeError('test compilation failed')
            Path(args[args.index('--iidfile') + 1]).write_text(image_id)
        if args[0] == 'rm':
            removed.append(args[-1])
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    manifest = tmp_path / 'session/tests/image.json'
    environment = ActivityEnvironment()
    if failure:
        expected = {'agent': 'model unavailable', 'build': 'test compilation failed',
                    'empty': 'did not produce a test suite', 'cancel': None,
                    'timeout': None}[failure]
        exception = {'cancel': asyncio.CancelledError,
                     'timeout': TimeoutError}.get(failure, RuntimeError)
        with pytest.raises(exception, match=expected) as raised:
            await environment.run(activities.generate_service_tests, 'session', context)
        assert not manifest.exists() and not (manifest.parent / 'suite').exists()
        if failure == 'agent':
            assert '[redacted]' in (manifest.parent / 'agent.log').read_text()
            assert 'test-secret-key' not in str(raised.value)
    else:
        result = await environment.run(activities.generate_service_tests, 'session', context)
        assert json.loads(manifest.read_text()) == result
        assert result['image_id'] == image_id
        assert (Path(result['path']) / 'src/test/java/example/ContractTest.java').is_file()
    assert runner_built == (failure in {None, 'build'})
    assert removed == [container_name]
    assert stopped == ([(container_name, str(docker_agents.AGENT_STOP_GRACE_SECONDS))]
                       if failure == 'timeout' else [])
    assert private_source.read_text() == 'PRIVATE IMPLEMENTATION SOURCE'


async def test_invalid_session_is_rejected_before_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    with pytest.raises(ApplicationError, match='Invalid session ID'):
        await ActivityEnvironment().run(activities.generate_service_tests, '../escape',
                                        {'language': 'java'})
    assert list(tmp_path.iterdir()) == []


async def test_unsupported_language_is_rejected_before_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    for context in ({'api_design': empty_api()}, {'api_design': empty_api(), 'language': 'cobol'}):
        with pytest.raises(ApplicationError, match='Unsupported language'):
            await ActivityEnvironment().run(activities.generate_service_tests, 'session', context)
    assert list(tmp_path.iterdir()) == []


async def test_repair_continues_from_the_published_suite_with_the_trusted_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    context = {'requirements': [], 'api_design': empty_api(), 'language': 'java',
               'repair': {'attempt': 1, 'instructions': 'Expect 201.', 'test_failures': []}}
    with pytest.raises(ApplicationError, match='Generated tests are unavailable'):
        await ActivityEnvironment().run(activities.generate_service_tests, 'session', context)
    published = tmp_path / 'session/tests/suite'
    (published / 'src/test/java').mkdir(parents=True)
    (published / 'src/test/java/OrdersTest.java').write_text('previous test')
    (published / 'pom.xml').write_text('previous pom')
    (published / 'run-tests.sh').write_text('exit 0 # tampered on disk')
    sent = {}

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/tests/.'):
            tests = Path(args[2]) / 'src/test/java'
            tests.mkdir(parents=True)
            (tests / 'OrdersTest.java').write_text('repaired test')
            (Path(args[2]) / 'pom.xml').write_text('previous pom')
        if args[0] == 'build' and '--iidfile' in args:
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'd' * 64)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    result = await ActivityEnvironment().run(activities.generate_service_tests, 'session', context)
    assert sent['tests/src/test/java/OrdersTest.java'] == 'previous test'
    assert sent['tests/pom.xml'] == 'previous pom'
    assert sent['tests/run-tests.sh'] == (TEMPLATE / 'run-tests.sh').read_text()
    assert json.loads(sent['input.json'])['repair'] == context['repair']
    assert (Path(result['path']) / 'src/test/java/OrdersTest.java').read_text() == 'repaired test'


async def test_repository_session_continues_only_the_repositorys_contract_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    checkout = tmp_path / 'session/repository'
    for name, text in {'contract-tests/src/test/java/OrdersTest.java': 'existing contract test',
                       'contract-tests/run-tests.sh': 'exit 0 # from the repository',
                       'src/main/Orders.java': 'implementation'}.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    context = {'requirements': [], 'api_design': empty_api(), 'language': 'java',
               'repository': {'full_name': 'octocat/project', 'branch': 'main'}}
    with pytest.raises(ApplicationError, match='not recorded'):
        await ActivityEnvironment().run(activities.generate_service_tests, 'session', context)
    await record_repository_tests(tmp_path, monkeypatch)
    # The record is of the tests as they were first found: what a later stage or a
    # failed push leaves in the checkout does not replace it.
    (checkout / 'contract-tests/src/test/java/OrdersTest.java').write_text('rewritten since')
    await record_repository_tests(tmp_path, monkeypatch)
    sent = {}

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/tests/.'):
            tests = Path(args[2]) / 'src/test/java'
            tests.mkdir(parents=True)
            (tests / 'OrdersTest.java').write_text('test')
            (Path(args[2]) / 'pom.xml').write_text('pom')
        if args[0] == 'build' and '--iidfile' in args:
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'd' * 64)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    await ActivityEnvironment().run(activities.generate_service_tests, 'session', context)
    assert sent['tests/src/test/java/OrdersTest.java'] == 'existing contract test'
    assert sent['tests/run-tests.sh'] == (TEMPLATE / 'run-tests.sh').read_text()
    assert json.loads(sent['input.json'])['repository'] == context['repository']
    # The implementation in the same repository stays out of the test author's reach.
    assert not any('Orders.java' in name for name in sent)


def java_tests(**bodies: str) -> str:
    # "annotations|body" puts the annotations before @Test.
    methods = ''.join(f'    {body.rpartition("|")[0]}\n    @Test\n    void {name}() {{ {body.rpartition("|")[2]} }}\n'
                      for name, body in bodies.items())
    return f'package com.example;\n\nclass OrdersTest {{\n{methods}}}\n'


@pytest.mark.parametrize('broken, written, refused', [
    # A new case beside the existing ones, in their class.
    (None, {'lists': 'get();', 'refusesDoubleBooking': 'post();'}, None),
    (None, {'lists': 'get();'}, 'did not add a regression test'),
    (None, {'lists': 'weakened();', 'refusesDoubleBooking': 'post();'},
     'may not touch: com.example.OrdersTest#lists'),
    (None, {'refusesDoubleBooking': 'post();'}, 'may not touch: com.example.OrdersTest#lists'),
    # Switching a test off changes it as much as rewriting it.
    (None, {'lists': '@Disabled("flaky (sometimes)") public|get();', 'refusesDoubleBooking': 'post();'},
     'may not touch: com.example.OrdersTest#lists'),
    # A repair changes or removes the tests found broken, and only those.
    ('lists', {'lists': 'corrected();', 'refusesDoubleBooking': 'post();'}, None),
    ('lists', {'refusesDoubleBooking': 'post();'}, None),
    ('lists', {'lists': 'get();', 'refusesDoubleBooking': 'weakened();'},
     'may not touch: com.example.OrdersTest#refusesDoubleBooking'),
])
async def test_only_tests_found_broken_may_differ_from_what_they_were(tmp_path, monkeypatch, broken, written, refused):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    source = 'src/test/java/com/example/OrdersTest.java'
    existing = tmp_path / 'session/repository/contract-tests' / source
    existing.parent.mkdir(parents=True)
    existing.write_text(java_tests(lists='get();'))
    assert await record_repository_tests(tmp_path, monkeypatch) == {'tests': 1,
                                                                    'component_tests': 0}
    brief: list[Requirement] = [
        {'id': 'FR-1', 'category': 'functional', 'title': 'No double booking',
         'description': 'A booked room is refused.', 'priority': 'must'}]
    context = {'requirements': [], 'api_design': empty_api(), 'language': 'java',
               'repository': {'full_name': 'octocat/project', 'branch': 'main'},
               'brief': {'requirements': brief, 'requirements_document':
                         render_requirements_markdown(brief, 'Seen on Fridays.')}}
    if broken:
        # The suite the change published, of which the diagnosis condemned one test.
        published = tmp_path / 'session/tests/suite' / source
        published.parent.mkdir(parents=True)
        published.write_text(java_tests(lists='get();', refusesDoubleBooking='post();'))
        (tmp_path / 'session/tests/suite/pom.xml').write_text('pom')
        context['repair'] = {'attempt': 1, 'broken_tests': [
            {'class_name': 'com.example.OrdersTest', 'name': broken}]}
    sent = {}

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update(json.loads(archive.extractfile('input.json').read()))
        if args[0] == 'cp' and args[1].endswith(':/workspace/tests/.'):
            (Path(args[2]) / source).parent.mkdir(parents=True)
            # Laid out differently, which changes no test.
            (Path(args[2]) / source).write_text(java_tests(**written).replace('    ', '\t'))
            (Path(args[2]) / 'pom.xml').write_text('pom')
        if args[0] == 'build' and '--iidfile' in args:
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'd' * 64)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    run = ActivityEnvironment().run(activities.generate_service_tests, 'session', context)
    if refused:
        with pytest.raises(RuntimeError, match=refused):
            await run
        return
    result = await run
    assert sent['brief'] == {'requirements': brief, 'requirements_narrative': 'Seen on Fridays.'}
    # The baseline run holds each test to what the change did to it.
    changes = {key: result[key] for key in ('added_tests', 'changed_tests', 'removed_tests')}
    assert changes == {
        'added_tests': ['com.example.OrdersTest#refusesDoubleBooking'],
        'changed_tests': ['com.example.OrdersTest#lists'] if written.get('lists') == 'corrected();' else [],
        'removed_tests': [] if 'lists' in written else ['com.example.OrdersTest#lists']}


@pytest.mark.parametrize('written, refused', [
    ('get();', None),
    ('changed();', 'may not touch: com.example.OrdersTest#lists'),
])
async def test_a_review_correction_puts_back_the_repository_tests_it_is_given(tmp_path, monkeypatch, written, refused):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    source = 'src/test/java/com/example/OrdersTest.java'
    existing = tmp_path / 'session/repository/contract-tests' / source
    existing.parent.mkdir(parents=True)
    existing.write_text(java_tests(lists='get();'))
    await record_repository_tests(tmp_path, monkeypatch)
    # The API change rewrote the test, which its corrected service passes again as it was.
    published = tmp_path / 'session/tests/suite' / source
    published.parent.mkdir(parents=True)
    published.write_text(java_tests(lists='changed();'))
    (tmp_path / 'session/tests/suite/pom.xml').write_text('pom')
    case = 'com.example.OrdersTest#lists'
    context = {'requirements': [], 'api_design': empty_api(), 'language': 'java',
               'repository': {'full_name': 'octocat/project', 'branch': 'main'},
               'repair': {'attempt': 1, 'review': {
                   'findings': [{'summary': 'Put it back.', 'reason': 'Still passes.'}],
                   'restore': [case]}}}
    sent = {}

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update(json.loads(archive.extractfile('input.json').read()))
        if args[0] == 'cp' and args[1].endswith(':/workspace/tests/.'):
            (Path(args[2]) / source).parent.mkdir(parents=True)
            (Path(args[2]) / source).write_text(java_tests(lists=written))
            (Path(args[2]) / 'pom.xml').write_text('pom')
        if args[0] == 'build' and '--iidfile' in args:
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'd' * 64)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    run = ActivityEnvironment().run(activities.generate_service_tests, 'session', context)
    if refused:
        with pytest.raises(RuntimeError, match=refused):
            await run
    else:
        assert (await run)['changed_tests'] == []
    # The author is given the test as the repository holds it.
    assert set(sent['repair']['review']['restore']) == {case}
    assert 'get();' in sent['repair']['review']['restore'][case]


async def run_api_change_author(tmp_path, monkeypatch, published: dict[str, str],
                                written: dict[str, str], repair: dict) -> tuple[dict, Any]:
    """Run the test author of an API change whose repository holds `lists` and `books`,
    after the change published `published`; it writes `written`. Returns its input and
    the run, still to be awaited."""
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    source = 'src/test/java/com/example/OrdersTest.java'
    existing = tmp_path / 'session/repository/contract-tests' / source
    existing.parent.mkdir(parents=True)
    existing.write_text(java_tests(lists='get();', books='book();'))
    await record_repository_tests(tmp_path, monkeypatch)
    suite = tmp_path / 'session/tests/suite' / source
    suite.parent.mkdir(parents=True)
    suite.write_text(java_tests(**published))
    (tmp_path / 'session/tests/suite/pom.xml').write_text('pom')
    context = {'requirements': [], 'api_design': empty_api(), 'language': 'java',
               'repository': {'full_name': 'octocat/project', 'branch': 'main'},
               'repair': repair, 'keep': ['com.example.OrdersTest#lists']}
    sent = {}

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update(json.loads(archive.extractfile('input.json').read()))
        if args[0] == 'cp' and args[1].endswith(':/workspace/tests/.'):
            (Path(args[2]) / source).parent.mkdir(parents=True)
            (Path(args[2]) / source).write_text(java_tests(**written))
            (Path(args[2]) / 'pom.xml').write_text('pom')
        if args[0] == 'build' and '--iidfile' in args:
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'd' * 64)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    return sent, ActivityEnvironment().run(activities.generate_service_tests, 'session', context)


@pytest.mark.parametrize('written, refused', [
    # The kept test stays; what made it fail in the suite is corrected around it.
    ({'lists': 'get();', 'books': 'book();', 'added': 'post();'}, None),
    ({'lists': 'rewritten();', 'books': 'book();', 'added': 'post();'},
     'may not touch: com.example.OrdersTest#lists'),
    ({'books': 'book();', 'added': 'post();'}, 'may not touch: com.example.OrdersTest#lists'),
])
async def test_a_repair_leaves_the_tests_the_changed_service_was_seen_to_pass(tmp_path, monkeypatch, written, refused):
    # The diagnosis found the kept test broken in the suite of the API change.
    sent, run = await run_api_change_author(
        tmp_path, monkeypatch, {'lists': 'get();', 'books': 'book();', 'added': 'post();'},
        written, {'attempt': 1, 'broken_tests': [
            {'class_name': 'com.example.OrdersTest', 'name': 'lists'}]})
    if refused:
        with pytest.raises(RuntimeError, match=refused):
            await run
    else:
        assert (await run)['changed_tests'] == []
    # The author is told which tests those are, as the repository holds them.
    assert set(sent['keep']) == {'com.example.OrdersTest#lists'}
    assert 'get();' in sent['keep']['com.example.OrdersTest#lists']


@pytest.mark.parametrize('written, refused', [
    ({'lists': 'get();', 'books': 'book();'}, None),
    # Putting one test back is no occasion to rewrite another the suite had left alone.
    ({'lists': 'get();', 'books': 'rewritten();'}, 'may not touch: com.example.OrdersTest#books'),
])
async def test_putting_tests_back_leaves_the_other_repository_tests_alone(tmp_path, monkeypatch, written, refused):
    sent, run = await run_api_change_author(
        tmp_path, monkeypatch, {'lists': 'changed();', 'books': 'book();'}, written,
        {'attempt': 1, 'review': {'findings': [{'summary': 'Put it back.', 'reason': 'Kept.'}],
                                  'restore': ['com.example.OrdersTest#lists']}})
    if refused:
        with pytest.raises(RuntimeError, match=refused):
            await run
    else:
        assert (await run)['changed_tests'] == []
