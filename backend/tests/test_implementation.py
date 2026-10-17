import json
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.implementation import activities
from app.languages import get_language


@pytest.mark.parametrize('previous', [False, True])
@pytest.mark.parametrize('failure', [None, 'build', 'agent', 'empty', 'image_id'])
async def test_implementation_publishes_only_successful_images(tmp_path, monkeypatch, failure, previous):
    """Owns Docker lifecycle, test isolation and durable image publication."""
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    monkeypatch.setattr(docker_agents, 'OPENAI_API_KEY', 'test-secret-key')
    source = tmp_path / 'session' / 'server'
    (source / 'src/main').mkdir(parents=True)
    (source / 'src/main/Service.java').write_text('generated interface')
    (source / 'src/test').mkdir()
    (source / 'src/test/SecretTest.java').write_text('private tests')
    (source / 'linked-test').symlink_to(source / 'src/test/SecretTest.java')
    cleaned_up = False
    image_id = 'sha256:' + 'b' * 64

    async def docker(*args, **kwargs):
        nonlocal cleaned_up
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 1 if failure == 'agent' else 0})
        if args[0] == 'logs':
            return 'Provider rejected test-secret-key: model unavailable'

        if args[0] == 'cp' and args[1].endswith(':/workspace/service/.'):
            output = Path(args[2])
            # Stand in for the external agent's filesystem, not the publication layer.
            if failure != 'empty':
                (output / 'Dockerfile').write_text('FROM scratch')
            (output / 'Service.java').write_text('implemented')
            (output / '.agent-output').mkdir()
            (output / '.agent-output/shell-1.log').write_text('verbose build output')
        if args[0] == 'build' and '--iidfile' in args:
            context = Path(args[-1])
            assert (context / 'Service.java').read_text() == 'implemented'
            assert not (context / '.agent-output').exists()
            if failure == 'build':
                raise RuntimeError('build rejected')
            Path(args[args.index('--iidfile') + 1]).write_text(
                'invalid-image-id' if failure == 'image_id' else image_id,
            )
        if args[0] == 'cp' and args[1] == '-a':
            import tarfile
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                names = archive.getnames()
                assert 'service/src/main/Service.java' in names
                assert not any('SecretTest' in name or 'linked-test' in name for name in names)
                assert all(item.uid == 10001 for item in archive.getmembers())
        if args[0] == 'rm':
            cleaned_up = True
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    environment = ActivityEnvironment()
    manifest = tmp_path / 'session/implementation/image.json'
    destination = manifest.parent / 'service'
    old_result = {'image_id': 'sha256:' + 'a' * 64, 'path': str(destination), 'session_id': 'session'}
    if previous:
        destination.mkdir(parents=True)
        (destination / 'Previous.java').write_text('previous successful service')
        manifest.write_text(json.dumps(old_result))
    if failure:
        expected = {'agent': 'model unavailable', 'build': 'build rejected',
                    'empty': 'did not produce a Dockerfile', 'image_id': 'invalid service image ID'}[failure]
        with pytest.raises(RuntimeError, match=expected) as raised:
            await environment.run(activities.implement_service, 'session',
                                  {'requirements': [], 'language': 'java'})
        if previous:
            assert json.loads(manifest.read_text()) == old_result
            assert (destination / 'Previous.java').read_text() == 'previous successful service'
        else:
            assert not manifest.exists() and not destination.exists()
        if failure == 'agent':
            log = (manifest.parent / 'agent.log').read_text()
            assert 'model unavailable' in log and '[redacted]' in log
            assert 'test-secret-key' not in log
            assert 'test-secret-key' not in str(raised.value)
    else:
        result = await environment.run(activities.implement_service, 'session',
                                  {'requirements': [], 'language': 'java'})
        assert json.loads(manifest.read_text()) == result
        assert result['image_id'] == image_id
        assert (Path(result['path']) / 'Service.java').read_text() == 'implemented'
        assert not (destination / 'Previous.java').exists()
    assert cleaned_up
    assert (source / 'src/test/SecretTest.java').read_text() == 'private tests'


async def test_generated_sources_are_required_before_creating_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    with pytest.raises(ApplicationError, match='Generated server code is unavailable'):
        await ActivityEnvironment().run(activities.implement_service, 'session',
                                        {'language': 'java'})
    assert list(tmp_path.iterdir()) == []


async def test_unsupported_language_is_rejected_before_creating_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    for context in ({}, {'language': 'cobol'}):
        with pytest.raises(ApplicationError, match='Unsupported language'):
            await ActivityEnvironment().run(activities.implement_service, 'session', context)
    assert list(tmp_path.iterdir()) == []


async def test_repair_continues_from_the_published_service(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    (tmp_path / 'session/server').mkdir(parents=True)
    (tmp_path / 'session/server/Api.java').write_text('generated skeleton')
    published = tmp_path / 'session/implementation/service'
    published.mkdir(parents=True)
    (published / 'Service.java').write_text('previous implementation')
    repair = {'attempt': 1, 'instructions': 'Return 404 for unknown orders.', 'test_failures': []}
    sent = {}

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            import tarfile
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/service/.'):
            (Path(args[2]) / 'Dockerfile').write_text('FROM scratch')
            (Path(args[2]) / 'Service.java').write_text('repaired implementation')
        if args[0] == 'build' and '--iidfile' in args:
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'c' * 64)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    result = await ActivityEnvironment().run(
        activities.implement_service, 'session', {'requirements': [], 'language': 'java', 'repair': repair})
    assert sent['service/Service.java'] == 'previous implementation'
    assert 'service/Api.java' not in sent
    assert json.loads(sent['input.json'])['repair'] == repair
    assert (Path(result['path']) / 'Service.java').read_text() == 'repaired implementation'


async def test_session_with_a_repository_is_implemented_inside_its_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    generated = tmp_path / 'session/server'
    (generated / 'src/main').mkdir(parents=True)
    (generated / 'src/main/Api.java').write_text('generated contract')
    (generated / 'pom.xml').write_text('generated pom')
    (generated / 'README.md').write_text('generated readme')
    checkout = tmp_path / 'session/repository'
    for name, text in {'src/main/Api.java': 'outdated contract', 'src/main/Orders.java': 'existing code',
                       'pom.xml': 'project pom', 'src/test/OrdersTest.java': 'project tests',
                       'contract-tests/pom.xml': 'contract tests',
                       'contracts/api.json': 'published contract', '.git/config': 'origin'}.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    (checkout / 'mvnw').write_text('#!/bin/sh')
    (checkout / 'mvnw').chmod(0o755)
    context = {'requirements': [], 'language': 'java', 'repository': {'full_name': 'octocat/project', 'branch': 'main'}}
    sent = {}
    executable = {}

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            import tarfile
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
                executable['agent'] = archive.getmember('service/mvnw').mode & 0o111
                assert not archive.getmember('service/pom.xml').mode & 0o111
        if args[0] == 'cp' and args[1].endswith(':/workspace/service/.'):
            (Path(args[2]) / 'Dockerfile').write_text('FROM scratch')
            (Path(args[2]) / 'mvnw').write_text('#!/bin/sh')
            (Path(args[2]) / 'mvnw').chmod(0o755)
        if args[0] == 'build' and '--iidfile' in args:
            executable['build'] = (Path(args[-1]) / 'mvnw').stat().st_mode & 0o111
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'c' * 64)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    await ActivityEnvironment().run(activities.implement_service, 'session', context)
    assert json.loads(sent.pop('input.json'))['repository'] == context['repository']
    # A checked-in build helper stays runnable for the agent and the image build.
    assert sent.pop('service/mvnw') == '#!/bin/sh' and executable['agent'] and executable['build']
    # The repository's own code, with the generated contract replacing its sources and
    # filling gaps only; never Git metadata or either kind of tests.
    # The stack is the registered language's, not the author's choice.
    assert json.loads(sent.pop('language.json')) == {
        'name': 'java', 'build_dirs': ['target'], 'instructions': get_language('java').implementation.instructions}
    assert sent == {'service/src/main/Api.java': 'generated contract',
                    'service/src/main/Orders.java': 'existing code',
                    'service/pom.xml': 'project pom', 'service/README.md': 'generated readme'}
    # The checkout itself changes only when verified sources are pushed.
    assert (checkout / 'src/main/Api.java').read_text() == 'outdated contract'

    import shutil
    shutil.rmtree(checkout)
    with pytest.raises(ApplicationError, match='repository checkout is unavailable'):
        await ActivityEnvironment().run(activities.implement_service, 'session', context)


@pytest.mark.parametrize('repository, expected', [
    # A project of its own, laid out unlike generated code: the API is implemented in it,
    # and none of the generated scaffolding reaches the branch.
    ({'backend/app/Main.java': 'existing service', 'README.md': 'project readme'},
     {'service/backend/app/Main.java': 'existing service', 'service/README.md': 'project readme'}),
    # A repository with no project yet starts from the generated one.
    ({'README.md': 'project readme', 'LICENSE': 'MIT', '.gitignore': 'target/'},
     {'service/src/main/Api.java': 'generated contract', 'service/pom.xml': 'generated pom',
      'service/README.md': 'project readme', 'service/LICENSE': 'MIT',
      'service/.gitignore': 'target/'}),
])
async def test_generated_code_joins_only_a_checkout_it_fits(tmp_path, monkeypatch,
                                                            repository, expected):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    generated = tmp_path / 'session/server'
    for name, text in {'src/main/Api.java': 'generated contract', 'pom.xml': 'generated pom',
                       'README.md': 'generated readme'}.items():
        (generated / name).parent.mkdir(parents=True, exist_ok=True)
        (generated / name).write_text(text)
    checkout = tmp_path / 'session/repository'
    for name, text in repository.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    context = {'requirements': [], 'language': 'java',
               'repository': {'full_name': 'octocat/project', 'branch': 'main'}}
    sent = {}

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            import tarfile
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/service/.'):
            (Path(args[2]) / 'Dockerfile').write_text('FROM scratch')
        if args[0] == 'build' and '--iidfile' in args:
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'c' * 64)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    await ActivityEnvironment().run(activities.implement_service, 'session', context)
    # The author is told whether the generated contract is in the checkout.
    added = 'service/src/main/Api.java' in expected
    assert json.loads(sent.pop('input.json'))['generated_code'] is added
    del sent['language.json']
    assert sent == expected


async def test_change_edits_the_checkout_without_generated_code(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    checkout = tmp_path / 'session/repository'
    for name, text in {'src/main/Api.java': 'published contract code', 'Dockerfile': 'FROM scratch',
                       'src/test/OrdersTest.java': 'project tests',
                       'contract-tests/pom.xml': 'contract tests',
                       'contracts/api.json': 'published contract'}.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    brief = {'requirements': [], 'requirements_document': 'Rooms double-book.'}
    context = {'requirements': [], 'language': 'java', 'brief': brief,
               'repository': {'full_name': 'octocat/project', 'branch': 'main'}}
    sent = {}

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            import tarfile
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/service/.'):
            (Path(args[2]) / 'Dockerfile').write_text('FROM scratch')
        if args[0] == 'build' and '--iidfile' in args:
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'c' * 64)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    # No code was generated for the session, and none is needed.
    await ActivityEnvironment().run(activities.implement_service, 'session', context)
    assert json.loads(sent.pop('input.json'))['brief'] == brief
    del sent['language.json']
    # The repository's service as it is; never either kind of tests or the contract.
    assert sent == {'service/src/main/Api.java': 'published contract code',
                    'service/Dockerfile': 'FROM scratch'}
