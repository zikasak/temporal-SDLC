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
from app.test_generation import activities


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
               'api_design': empty_api(), 'implementation': private_source.read_text(),
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
        if args[0] == 'wait' and failure == 'cancel':
            raise asyncio.CancelledError()
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 1 if failure == 'agent' else 0})
        if args[0] == 'logs':
            return 'Provider rejected test-secret-key: model unavailable'
        if args[0] == 'cp' and args[1].endswith(':/workspace/tests/.'):
            output = Path(args[2])
            (output / 'pom.xml').write_text((activities.TEMPLATE / 'pom.xml').read_text())
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
            assert (build / 'Dockerfile').read_text() == (activities.TEMPLATE / 'Dockerfile').read_text()
            assert (build / 'run-tests.sh').read_text() == (activities.TEMPLATE / 'run-tests.sh').read_text()
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
                    'empty': 'did not produce a Maven test suite', 'cancel': None}[failure]
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
        await ActivityEnvironment().run(activities.generate_service_tests, '../escape', {})
    assert list(tmp_path.iterdir()) == []
