import json
import tarfile
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.documentation import activities

CONTEXT = {'requirements': [], 'language': 'java',
           'brief': {'requirements': [], 'requirements_document': 'Describe the rooms API.'},
           'repository': {'full_name': 'octocat/project', 'branch': 'main'}}


@pytest.mark.parametrize('changed', [True, False])
async def test_documentation_author_sees_and_returns_documentation_only(tmp_path, monkeypatch, changed):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    checkout = tmp_path / 'session/repository'
    for name, text in {'README.md': 'project', 'docs/guide.md': 'guide',
                       'src/main/Api.java': 'service code', 'Dockerfile': 'FROM scratch',
                       'target/site/index.md': 'build output',
                       'contract-tests/README.md': 'the test suite',
                       'contracts/requirements.md': 'published contract'}.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    sent = {}
    built = []

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/repository/.'):
            # The author rewrites one file, adds one and removes the guide, and
            # reaches for files that are not documentation.
            written = {'README.md': 'rewritten', 'docs/api.rst': 'added',
                       'src/main/Api.java': 'tampered', 'contracts/requirements.md': 'tampered'}
            for name, text in (written if changed else
                               {'README.md': 'project', 'docs/guide.md': 'guide'}).items():
                (Path(args[2]) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(args[2]) / name).write_text(text)
        if args[0] == 'build' and '--iidfile' in args:
            built.append(args)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    published = tmp_path / 'session/documentation/files'
    if not changed:
        with pytest.raises(RuntimeError, match='changed no documentation file'):
            await ActivityEnvironment().run(activities.write_documentation, 'session', CONTEXT)
        assert not published.exists()
        return
    result = await ActivityEnvironment().run(activities.write_documentation, 'session', CONTEXT)
    assert json.loads(sent.pop('input.json'))['brief'] == CONTEXT['brief']
    del sent['language.json']
    sent.pop('llm-settings.json')
    # Never the service, its build output, the contract tests or the published contract.
    assert sent == {'repository/README.md': 'project', 'repository/docs/guide.md': 'guide'}
    # Documentation is published as files: nothing is built from it.
    assert not built and 'image_id' not in result
    assert result['changed_files'] == ['README.md', 'docs/api.rst', 'docs/guide.md']
    assert {item.relative_to(published).as_posix(): item.read_text()
            for item in published.rglob('*') if item.is_file()} == {
        'README.md': 'rewritten', 'docs/api.rst': 'added'}
    # The checkout itself changes only when the documentation is pushed.
    assert (checkout / 'README.md').read_text() == 'project'
