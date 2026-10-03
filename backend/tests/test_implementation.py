import json
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.implementation import activities


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
            await environment.run(activities.implement_service, 'session', {'requirements': []})
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
        result = await environment.run(activities.implement_service, 'session', {'requirements': []})
        assert json.loads(manifest.read_text()) == result
        assert result['image_id'] == image_id
        assert (Path(result['path']) / 'Service.java').read_text() == 'implemented'
        assert not (destination / 'Previous.java').exists()
    assert cleaned_up
    assert (source / 'src/test/SecretTest.java').read_text() == 'private tests'


async def test_generated_sources_are_required_before_creating_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    with pytest.raises(ApplicationError, match='Generated server code is unavailable'):
        await ActivityEnvironment().run(activities.implement_service, 'session', {})
    assert list(tmp_path.iterdir()) == []
