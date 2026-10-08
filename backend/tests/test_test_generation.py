import asyncio
import json
import tarfile
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.api_design.models import empty_api
from app.api_design.openapi import render_openapi
from app.requirements.document import render_requirements_markdown
from app.languages import get_language
from app.test_generation import activities

TEMPLATE = get_language('java').test_suite.template


@pytest.mark.parametrize('failure', [None, 'agent', 'build', 'empty', 'cancel'])
async def test_contract_only_generation_publishes_compiled_suite(tmp_path, monkeypatch, failure):
    """Owns source isolation, Docker lifecycle and publication at the host boundary."""
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    monkeypatch.setattr(docker_agents, 'OPENAI_API_KEY', 'test-secret-key')
    monkeypatch.setattr(docker_agents, 'OPENAI_BASE_URL', 'http://localhost:1234/v1')
    implementation = tmp_path / 'session/implementation/service'
    implementation.mkdir(parents=True)
    private_source = implementation / 'Private.java'
    private_source.write_text('PRIVATE IMPLEMENTATION SOURCE')
    server = tmp_path / 'session/server'
    server.mkdir()
    (server / 'Generated.java').write_text('PRIVATE GENERATED SOURCE')
    requirements = [{'id': 'FR-1', 'category': 'functional', 'title': 'Create orders',
                     'description': 'Clients create orders.', 'priority': 'must',
                     'rationale': 'Core flow'}]
    context = {'requirements': requirements,
               'requirements_document': render_requirements_markdown(
                   requirements, 'Orders are never deleted.'),
               'api_design': empty_api(), 'language': 'java',
               'implementation': private_source.read_text(),
               'source_path': str(implementation)}
    image_id = 'sha256:' + 'c' * 64
    container_name = None
    removed = []
    runner_built = False

    async def docker(*args, **kwargs):
        nonlocal container_name, runner_built
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
                    'name': 'java', 'build_dirs': ['target'], 'instructions': get_language('java').test_suite.instructions,
                    'test_glob': 'src/test/java/**/*Test.java'}
        if args[0] == 'wait' and failure == 'cancel':
            raise asyncio.CancelledError()
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
                    'empty': 'did not produce a test suite', 'cancel': None}[failure]
        exception = asyncio.CancelledError if failure == 'cancel' else RuntimeError
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
