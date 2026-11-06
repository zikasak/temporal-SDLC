import json
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.implementation import activities, agent
from app.languages import OTHER_PARTS_BUILD_DIRS, get_language
from app.routing.models import SEAM_ERROR


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
        'name': 'java', 'build_dirs': ['target', *OTHER_PARTS_BUILD_DIRS], 'instructions': get_language('java').implementation.instructions}
    sent.pop('llm-settings.json')
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
    sent.pop('llm-settings.json')
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
    sent.pop('llm-settings.json')
    # The repository's service as it is; never either kind of tests or the contract.
    assert sent == {'service/src/main/Api.java': 'published contract code',
                    'service/Dockerfile': 'FROM scratch'}


@pytest.mark.parametrize('kind, told', [
    ({'brief': {'requirements': [], 'requirements_document': 'Show the order total.'}}, True),
    ({}, True),
    # An onboarding writes the service's Dockerfile and changes no other part.
    ({'onboarding': True}, False),
])
async def test_every_project_of_the_checkout_is_the_authors_to_change(tmp_path, monkeypatch,
                                                                      kind, told):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    (tmp_path / 'session/server').mkdir(parents=True)
    checkout = tmp_path / 'session/repository'
    for name, text in {'backend/requirements.txt': 'fastapi', 'backend/app/main.py': 'app',
                       'frontend/package.json': '{}', 'frontend/src/App.tsx': 'page',
                       'frontend/node_modules/react/package.json': '{}',
                       'contract-tests/requirements.txt': 'pytest', 'Dockerfile': 'FROM scratch',
                       'README.md': 'project readme'}.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    context = {'requirements': [], 'language': 'python', **kind,
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
    inputs = json.loads(sent['input.json'])
    # The frontend reaches the author beside the service; its dependencies and the
    # contract tests never do.
    assert sent['service/frontend/src/App.tsx'] == 'page'
    assert not any('node_modules' in name or 'contract-tests' in name for name in sent)
    assert inputs.get('repository_parts') == ([
        {'path': 'backend', 'toolchain': 'Python'},
        {'path': 'frontend', 'toolchain': 'Node.js (npm)'}] if told else None)
    prompt = agent.base_prompt(inputs, '')
    assert (agent.PARTS_PROMPT.splitlines()[1] in prompt) is told


async def test_a_change_proved_at_seams_gets_the_stubs_it_has_to_keep(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    session = tmp_path / 'session'
    for name, text in {'repository/app/rooms.py': 'code', 'repository/Dockerfile': 'FROM scratch',
                       'repository/component-tests/tests/test_rooms.py': 'component tests',
                       'repository/contracts/components/app/rooms.pyi': 'published stub',
                       'routing/components/app/rooms.pyi': 'routed stub',
                       'routing/components/runtime.json': '{}'}.items():
        (session / name).parent.mkdir(parents=True, exist_ok=True)
        (session / name).write_text(text)
    routes = {'FR-2': {'route': 'component', 'reason': 'R.',
                       'seam': {'module': 'app.rooms', 'symbols': ['close']}}}
    context = {'requirements': [], 'language': 'python', 'component_routes': routes,
               'brief': {'requirements': [], 'requirements_document': 'Close rooms.'},
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
    inputs = json.loads(sent['input.json'])
    assert inputs['component_routes'] == routes
    # The stubs the routing checked, beside the checkout; never the component tests.
    assert sent['contracts/components/app/rooms.pyi'] == 'routed stub'
    assert not any('component-tests' in name or name.startswith('service/contracts')
                   for name in sent)
    prompt = agent.base_prompt(inputs, '')
    assert agent.COMPONENT_PROMPT in prompt and 'keep every signature' in prompt
    assert agent.COMPONENT_PROMPT not in agent.base_prompt({'brief': {}}, '')

    # Without the routing's stubs nothing can be held to them.
    import shutil
    shutil.rmtree(session / 'routing/components')
    with pytest.raises(ApplicationError, match='component contract of the routing'):
        await ActivityEnvironment().run(activities.implement_service, 'session', context)


@pytest.mark.parametrize('conforms', [True, False])
async def test_a_seam_the_code_lacks_is_added_alone_and_checked_against_its_stubs(
        tmp_path, monkeypatch, conforms):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    session = tmp_path / 'session'
    runtime = json.dumps({'source_root': '.', 'requirements': []})
    for name, text in {'repository/app/rooms.py': 'code', 'repository/Dockerfile': 'FROM scratch',
                       'routing/components/app/rooms.pyi': 'routed stub',
                       'routing/components/runtime.json': runtime}.items():
        (session / name).parent.mkdir(parents=True, exist_ok=True)
        (session / name).write_text(text)
    routes = {'FR-2': {'route': 'component', 'reason': 'R.',
                       'seam': {'module': 'app.rooms', 'symbols': ['merged'],
                                'added': ['merged']}}}
    brief = {'requirements': [], 'requirements_document': 'Close merged rooms.'}
    context = {'requirements': [], 'language': 'python', 'component_routes': routes,
               'brief': brief, 'seam': {'app.rooms': ['merged']},
               'repository': {'full_name': 'octocat/project', 'branch': 'main'}}
    sent, runs = {}, []

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            import tarfile
            sent.clear()
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/service/.'):
            (Path(args[2]) / 'Dockerfile').write_text('FROM scratch')
            (Path(args[2]) / 'app').mkdir()
            (Path(args[2]) / 'app/rooms.py').write_text('code with the seam')
        if args[0] == 'build' and '--iidfile' in args:
            runs.append(('build', Path(args[-1])))
            if (Path(args[-1]) / 'repository').is_dir():
                runs.append(('checked', (Path(args[-1]) / 'repository/app/rooms.py').read_text()))
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'c' * 64)
        if args[0] == 'run':
            runs.append(('run', args[-1]))
            if not conforms:
                raise RuntimeError('Docker run failed: error: app.rooms.merged is not present '
                                   'at runtime')
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    result = await ActivityEnvironment().run(activities.implement_service, 'session', context)
    inputs = json.loads(sent['input.json'])
    assert inputs['seam'] == {'app.rooms': ['merged']}
    # The author starts from the repository's code, with the stubs beside it.
    assert sent['service/app/rooms.py'] == 'code'
    assert sent['contracts/components/app/rooms.pyi'] == 'routed stub'
    # It is told to add the seam alone, not to fix anything.
    prompt = agent.base_prompt(inputs, '')
    assert prompt.startswith(agent.SEAM_PROMPT.splitlines()[0])
    assert agent.COMPONENT_PROMPT not in prompt and agent.CHANGE_PROMPT.splitlines()[0] not in prompt
    # The code with the seam is published apart from the fix, built, then checked against
    # the whole component contract.
    assert (session / 'seam/service/app/rooms.py').read_text() == 'code with the seam'
    assert ('checked', 'code with the seam') in runs and ('run', 'app.rooms') in runs
    if not conforms:
        assert result == {'session_id': 'session', 'conformance_error': (
            'The component contract does not match the code: error: app.rooms.merged is not '
            'present at runtime')}
        return
    assert result['image_id'] == 'sha256:' + 'c' * 64
    assert result['path'] == str(session / 'seam/service')

    # The fix then starts from the code with the seam, not from the repository's.
    fix = {key: value for key, value in context.items() if key != 'seam'}
    await ActivityEnvironment().run(activities.implement_service, 'session',
                                    {**fix, 'seam_prepared': True})
    assert sent['service/app/rooms.py'] == 'code with the seam'
    assert agent.COMPONENT_PROMPT in agent.base_prompt(json.loads(sent['input.json']), '')


@pytest.mark.parametrize('failure, reason', [
    ('agent', 'The seam agent did not finish (RuntimeError): what it said is in agent.log of '
              "the session's seam stage."),
    ('output', 'The seam agent left no Dockerfile at the root of the repository.'),
    ('build', 'The service image could not be built from the root Dockerfile of the code '
              'with the seam: ERROR: requirements.txt not found.'),
    ('check', 'The code with the seam could not be checked against its component contract '
              '(FileNotFoundError).'),
    ('contract', 'The component contract of the routing is unavailable.'),
])
async def test_a_seam_that_cannot_be_added_says_which_step_failed(
        tmp_path, monkeypatch, failure, reason):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    session = tmp_path / 'session'
    files = {'repository/app/rooms.py': 'code', 'repository/Dockerfile': 'FROM scratch',
             'routing/components/app/rooms.pyi': 'routed stub',
             'routing/components/runtime.json': json.dumps(
                 {'source_root': '.', 'requirements': []})}
    if failure == 'check':
        del files['routing/components/runtime.json']
    if failure == 'contract':
        files = {name: text for name, text in files.items()
                 if not name.startswith('routing/')}
    for name, text in files.items():
        (session / name).parent.mkdir(parents=True, exist_ok=True)
        (session / name).write_text(text)
    routes = {'FR-2': {'route': 'component', 'reason': 'R.',
                       'seam': {'module': 'app.rooms', 'symbols': ['merged'],
                                'added': ['merged']}}}
    context = {'requirements': [], 'language': 'python', 'component_routes': routes,
               'brief': {'requirements': [], 'requirements_document': 'Close merged rooms.'},
               'seam': {'app.rooms': ['merged']},
               'repository': {'full_name': 'octocat/project', 'branch': 'main'}}

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            # The agent's own words, which the user is not shown.
            return json.dumps({'ExitCode': 1 if failure == 'agent' else 0})
        if args[0] == 'logs':
            return 'secret provider output'
        if args[0] == 'cp' and args[1].endswith(':/workspace/service/.'):
            if failure != 'output':
                (Path(args[2]) / 'Dockerfile').write_text('FROM scratch')
        if args[0] == 'build' and '--iidfile' in args:
            if failure == 'build':
                raise RuntimeError('Docker build failed: ERROR: requirements.txt not found')
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'c' * 64)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(activities.implement_service, 'session', context)
    assert raised.value.type == SEAM_ERROR and raised.value.non_retryable
    assert raised.value.message == reason


@pytest.mark.parametrize('conforms', [True, False])
async def test_a_seam_of_the_web_client_is_added_and_checked_against_that_parts_stubs(
        tmp_path, monkeypatch, conforms):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    session = tmp_path / 'session'
    stub = 'export declare function sessionAttention(flow: unknown): string[];\n'
    for name, text in {
            'repository/app/rooms.py': 'code', 'repository/Dockerfile': 'FROM scratch',
            'repository/frontend/src/Flows.tsx': 'function sessionAttention() {}',
            'routing/routes.json': json.dumps({'routes': {}, 'parts': [
                {'name': 'frontend', 'root': 'frontend', 'language': 'typescript'}]}),
            'routing/parts/frontend/components/src/Flows.d.ts': stub,
            'routing/parts/frontend/components/runtime.json': '{"project": "frontend"}'}.items():
        (session / name).parent.mkdir(parents=True, exist_ok=True)
        (session / name).write_text(text)
    seam = {'part': 'frontend', 'module': 'src/Flows', 'symbols': ['sessionAttention'],
            'added': ['sessionAttention']}
    context = {'requirements': [], 'language': 'python',
               'component_routes': {'FR-115': {'route': 'component', 'reason': 'R.',
                                               'seam': seam}},
               'brief': {'requirements': [], 'requirements_document': 'Mark closed flows.'},
               'seam': {'frontend/src/Flows': ['sessionAttention']},
               'repository': {'full_name': 'octocat/project', 'branch': 'main'}}
    sent, runs = {}, []

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
            (Path(args[2]) / 'frontend/src').mkdir(parents=True)
            (Path(args[2]) / 'frontend/src/Flows.tsx').write_text(
                'export function sessionAttention() {}')
        if args[0] == 'build' and '--iidfile' in args:
            context = Path(args[-1])
            if (context / 'check.mjs').is_file():
                runs.append(('checked', (context / 'repository/frontend/src/Flows.tsx').read_text(),
                             (context / 'components/src/Flows.d.ts').read_text()))
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'c' * 64)
        if args[0] == 'run':
            runs.append(('run', args[-1]))
            if not conforms:
                raise RuntimeError('Docker run failed: src/Flows: sessionAttention is not '
                                   'exported by the code')
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    result = await ActivityEnvironment().run(activities.implement_service, 'session', context)
    # The author gets the part's stubs where its prompt says, and is told of part seams.
    assert sent['contracts/parts/frontend/components/src/Flows.d.ts'] == stub
    assert not any(name.startswith('contracts/components') for name in sent)
    assert 'contracts/parts/<part>/components' in agent.SEAM_PROMPT
    # The code with the seam is checked against the part's contract.
    assert ('checked', 'export function sessionAttention() {}', stub) in runs
    assert ('run', 'src/Flows') in runs
    if conforms:
        assert result['path'] == str(session / 'seam/service')
    else:
        assert result == {'session_id': 'session', 'conformance_error': (
            'Part frontend: The component contract does not match the code: src/Flows: '
            'sessionAttention is not exported by the code')}
