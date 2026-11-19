"""The component suite of a change whose criteria are routed to seams of other parts of
its repository than the service, such as its web client: one project per part, written by
one author, built into one runner and run as one suite (plans/part-component-contracts.md,
step 3)."""
# Assertions read optional fields directly: a None there fails the test anyway.
# pyright: reportOptionalMemberAccess=false
import dataclasses
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.api_design.models import empty_api
from app.languages import TestReports as Reports, get_language, get_part_language
from app.languages.composite import CompositeSuite
from app.routing.parts import (
    SERVICE_PART, ComponentPart, component_suite, read_routed_parts, routed_parts,
)
from app.test_generation import activities as contract_tests
from app.test_generation import components
from app.verification import activities as verification
from app.verification.models import TrackedTest

PYTHON = get_language('python')
TYPESCRIPT = get_part_language('typescript')
SERVICE = ComponentPart(SERVICE_PART, '.', PYTHON)
FRONTEND = ComponentPart('frontend', 'frontend', TYPESCRIPT)
TEMPLATE = TYPESCRIPT.component_suite.template
IMAGE = 'sha256:' + 'e' * 64

REQUIREMENT = {'category': 'functional', 'title': 'T', 'description': 'D', 'priority': 'must'}
BRIEF = {'requirements': [{**REQUIREMENT, 'id': 'FR-2'}, {**REQUIREMENT, 'id': 'TR-6'}],
         'requirements_document': '# Brief', 'retired': []}
PART_SEAM = {'part': 'frontend', 'module': 'src/github', 'symbols': ['createGitHubApp']}
SERVICE_SEAM = {'module': 'service.rooms', 'symbols': ['Rooms.close']}
GITHUB_STUB = ('/** Subscribes the app to pull_request events. */\n'
               'export declare function createGitHubApp(name: string, organization: string): '
               'void;\n')
ROOMS_STUB = '''class Rooms:
    def close(self, room: str, merged: bool) -> None:
        """Closes the room, whether its pull request was merged or not."""
        ...
'''
EXISTING = '''import { expect, test } from 'vitest';
import { createGitHubApp } from '@part/src/github';

test('creates an app', () => expect(createGitHubApp).toBeDefined());
'''
MANIFEST = '''import { describe, expect, it } from 'vitest';
import { createGitHubApp } from '@part/src/github';

describe('the GitHub App manifest', () => {
  it('subscribes the app to pull_request events', () => {
    expect(createGitHubApp).toBeDefined();
  });
});
'''
ADDED = 'frontend:tests/manifest.test.ts#the GitHub App manifest > subscribes the app to ' \
        'pull_request events'
PYTHON_TEST = '''from service.rooms import Rooms


def test_a_merged_room_is_closed():
    Rooms().close("a", merged=True)
'''


def composite(*parts: ComponentPart) -> CompositeSuite:
    suite = component_suite(PYTHON, list(parts))
    assert isinstance(suite, CompositeSuite)
    return suite


def write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)


# ---- the suite of several parts ----

def test_the_service_alone_keeps_its_own_suite():
    assert component_suite(PYTHON, [SERVICE]) is PYTHON.component_suite


def test_each_part_has_a_project_and_its_tests_are_named_after_it(tmp_path):
    suite = composite(SERVICE, FRONTEND)
    write(tmp_path, {'tests/test_rooms.py': PYTHON_TEST,
                     'parts/frontend/tests/manifest.test.ts': MANIFEST,
                     'parts/frontend/package.json': '{}',
                     'parts/frontend/package-lock.json': '{}'})
    assert set(suite.test_cases(tmp_path)) == {'tests.test_rooms#test_a_merged_room_is_closed',
                                               ADDED}
    # A report entry is the part's when its class name starts with the part's prefix, as
    # the composite runner writes it.
    assert suite.case_id('frontend:tests/manifest.test.ts',
                         'the GitHub App manifest > subscribes the app to pull_request '
                         'events') == ADDED
    assert suite.case_id('tests.test_rooms', 'test_a_merged_room_is_closed') == (
        'tests.test_rooms#test_a_merged_room_is_closed')
    # Each part's files are published in its project; a part needs tests of its own.
    assert {'tests', 'pytest.ini', 'parts/frontend/tests',
            'parts/frontend/package.json'} <= set(suite.published)
    assert set(suite.required) >= {'parts/frontend/package.json',
                                   'parts/frontend/package-lock.json'}
    assert suite.has_tests(tmp_path)
    (tmp_path / 'tests/test_rooms.py').unlink()
    assert not suite.has_tests(tmp_path)
    # A test of a part that cannot be tracked is named with its part.
    write(tmp_path, {'parts/frontend/tests/each.test.ts':
                     "import { test } from 'vitest';\ntest.each([1])('x %s', () => {});\n"})
    untracked, = suite.untracked_tests(tmp_path)
    assert untracked.startswith('frontend: tests/each.test.ts line 2: a parameterized test')
    # The author is told of every part's project and instructions.
    assert 'part frontend (TypeScript) at /workspace/tests/parts/frontend' in suite.instructions
    assert PYTHON.component_suite.instructions in suite.instructions
    assert TYPESCRIPT.component_suite.instructions in suite.instructions
    assert {tool.name for tool in suite.tools} >= {tool.name for tool in
                                                   TYPESCRIPT.component_suite.tools}


def test_a_suite_without_the_service_names_every_test_after_its_part(tmp_path):
    suite = composite(FRONTEND)
    write(tmp_path, {'tests/test_rooms.py': PYTHON_TEST,
                     'parts/frontend/tests/manifest.test.ts': MANIFEST})
    # The service's tests, which the suite has no project of, are not its tests.
    assert set(suite.test_cases(tmp_path)) == {ADDED}
    assert suite.case_id('tests.test_rooms', 'x') == 'tests.test_rooms#x'


def test_a_copy_of_the_suite_holds_only_what_each_part_publishes(tmp_path):
    suite = composite(FRONTEND)
    write(tmp_path / 'source', {'parts/frontend/tests/manifest.test.ts': MANIFEST,
                                'parts/frontend/package.json': '{}',
                                'parts/frontend/node_modules/x/index.js': 'x',
                                'parts/frontend/vitest.config.mjs': 'untrusted',
                                'parts/backend/tests/a.test.ts': 'not a part of the suite',
                                'tests/test_rooms.py': PYTHON_TEST})
    contract_tests.copy_test_suite(suite, tmp_path / 'source', tmp_path / 'copy')
    assert sorted(item.relative_to(tmp_path / 'copy').as_posix()
                  for item in (tmp_path / 'copy').rglob('*') if item.is_file()) == [
        'parts/frontend/package.json', 'parts/frontend/tests/manifest.test.ts']


# ---- the parts the routing recorded ----

def test_the_routing_names_the_parts_the_stages_after_it_read():
    assert routed_parts(PYTHON, None) == [SERVICE]
    assert routed_parts(PYTHON, {'routes': {}}) == [SERVICE]
    assert routed_parts(PYTHON, {'parts': [
        {'name': 'frontend', 'root': 'frontend', 'language': 'typescript'},
        {'name': 'service', 'root': '.', 'language': 'python'}]}) == [SERVICE, FRONTEND]


@pytest.mark.parametrize('parts, error', [
    ('frontend', 'other than as a list'),
    ([{'name': 'frontend'}], 'without its name, root and language'),
    ([{'name': '../x', 'root': '../x', 'language': 'typescript'}], 'invalid part'),
    ([{'name': 'a', 'root': 'b', 'language': 'typescript'}], 'invalid part'),
    ([{'name': 'web', 'root': 'web', 'language': 'cobol'}], "Unsupported part language"),
    ([{'name': 'web', 'root': 'web', 'language': 'typescript'}] * 2, 'names a part twice'),
])
def test_the_routings_parts_are_checked(parts, error):
    with pytest.raises(ValueError, match=error):
        routed_parts(PYTHON, {'parts': parts})


# ---- the author and the runner of a change with criteria of the web client ----

CONTEXT = {'requirements': [{**REQUIREMENT, 'id': 'FR-1'}],
           'requirements_document': '# Contract', 'language': 'python',
           'api_design': empty_api(), 'brief': BRIEF,
           'routes': {'TR-6': {'route': 'component', 'reason': 'Read the posted manifest.',
                               'seam': PART_SEAM}},
           'repository': {'full_name': 'octo/hotel'}}
SUITE_FILES = {'parts/frontend/tests/existing.test.ts': EXISTING,
               'parts/frontend/tests/manifest.test.ts': MANIFEST,
               'parts/frontend/package.json': (TEMPLATE / 'package.json').read_text(),
               'parts/frontend/package-lock.json': '{}',
               'parts/frontend/vitest.config.mjs': 'untrusted'}


def routed(session_dir: Path, parts: list[ComponentPart]) -> None:
    (session_dir / 'routing').mkdir(parents=True, exist_ok=True)
    (session_dir / 'routing/routes.json').write_text(json.dumps(
        {'routes': {}, 'parts': [part.record() for part in parts]}))


@pytest.fixture
async def session(tmp_path, monkeypatch):
    """A Python service with a TypeScript web client beside it, which has component tests
    of its own, routed and recorded as the delivery leaves it before its authors run."""
    for module in (components, verification, contract_tests):
        monkeypatch.setattr(module, 'GENERATED_CODE_DIR', tmp_path)
    session_dir = tmp_path / 'session'
    write(session_dir, {
        'repository/Dockerfile': 'FROM python:3.12-slim',
        'repository/requirements.txt': 'temporalio\n',
        'repository/src/service/rooms.py': 'PRIVATE SERVICE SOURCE',
        'repository/frontend/package.json': json.dumps({'devDependencies': {'typescript': '5'}}),
        'repository/frontend/package-lock.json': '{}',
        'repository/frontend/tsconfig.json': '{}',
        'repository/frontend/src/github.ts': 'PRIVATE CLIENT SOURCE',
        'repository/frontend/component-tests/tests/existing.test.ts': EXISTING,
        'repository/frontend/component-tests/package.json': '{}',
        'repository/frontend/component-tests/Dockerfile': 'FROM untrusted',
        'routing/parts/frontend/components/runtime.json': json.dumps({'project': 'frontend'}),
        'routing/parts/frontend/components/src/github.d.ts': GITHUB_STUB,
        'routing/components/runtime.json': json.dumps(
            {'source_root': 'src', 'requirements': ['requirements.txt']}),
        'routing/components/service/rooms.pyi': ROOMS_STUB,
    })
    routed(session_dir, [FRONTEND])
    recorded = await ActivityEnvironment().run(verification.snapshot_repository_tests,
                                               'session', 'python')
    assert recorded['component_tests_of_frontend'] == 1
    return session_dir


def fake_docker(monkeypatch, written: dict[str, str], sent: dict, calls: list,
                failing: str | None = None):
    """Stands in for the Docker CLI and the agent's filesystem: `written` is what the agent
    leaves in /workspace/tests, and a build whose context holds `failing` fails."""
    async def docker(*args, **kwargs):
        if args[0] == 'build' and '--iidfile' in args:
            context = Path(args[-1])
            files = {item.relative_to(context).as_posix(): item.read_text()
                     for item in context.rglob('*') if item.is_file()}
            calls.append(('build', args, files))
            if failing and failing in files:
                raise RuntimeError('vitest list failed')
            Path(args[args.index('--iidfile') + 1]).write_text(IMAGE)
            return ''
        calls.append(args)
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode(errors='replace')
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/tests/.'):
            write(Path(args[2]), written)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)


def builds(calls: list) -> list[tuple[tuple, dict[str, str]]]:
    # Not the agent's own image, which is built without an image ID file.
    return [(call[1], call[2]) for call in calls
            if call[0] == 'build' and isinstance(call[2], dict)]


def test_the_snapshot_records_each_parts_component_tests(session):
    record = session / 'repository-tests/components'
    assert (record / 'parts/frontend/tests/existing.test.ts').read_text() == EXISTING
    # Only what the part's suite publishes, as for the service's.
    assert not (record / 'parts/frontend/Dockerfile').exists()
    assert set(read_routed_parts(PYTHON, session)) == {FRONTEND}


async def test_the_author_writes_the_web_clients_tests_and_one_runner_runs_them(
        session, monkeypatch):
    sent: dict = {}
    calls: list = []
    fake_docker(monkeypatch, SUITE_FILES, sent, calls)
    result = await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                             CONTEXT)
    # The workspace: the part's template and its own component tests, in its project, and
    # its stubs, but no code and nothing of the service's suite.
    assert sent['tests/parts/frontend/tests/existing.test.ts'] == EXISTING
    # The repository's package.json, which the author continues, over the template's.
    assert sent['tests/parts/frontend/package.json'] == '{}'
    assert sent['tests/parts/frontend/package-lock.json'] == (
        TEMPLATE / 'package-lock.json').read_text()
    assert 'tests/parts/frontend/vitest.config.mjs' not in sent
    assert sent['contracts/parts/frontend/components/src/github.d.ts'] == GITHUB_STUB
    assert not any(name.startswith(('contracts/components', 'tests/tests', 'tests/pytest'))
                   for name in sent)
    assert not any('PRIVATE' in text for text in sent.values())
    payload = json.loads(sent['input.json'])
    assert payload['parts'] == [{'name': 'frontend', 'root': 'frontend',
                                 'language': 'TypeScript'}]
    assert payload['routes'] == CONTEXT['routes']
    language = json.loads(sent['language.json'])
    assert language['test_globs'] == ['parts/frontend/tests/**/*.test.ts*']
    assert 'part frontend (TypeScript)' in language['instructions']

    # The part's runner, built with the code as the delivery started from it, then the
    # one image that runs it.
    (member_args, member), (composite_args, files) = builds(calls)
    assert member['Dockerfile'] == (TEMPLATE / 'Dockerfile').read_text()
    assert member['repository/frontend/src/github.ts'] == 'PRIVATE CLIENT SOURCE'
    assert member['suite/tests/manifest.test.ts'] == MANIFEST
    assert 'suite/vitest.config.mjs' not in member
    assert 'PROJECT=frontend' in member_args
    tag, = [call[2] for call in calls if call[0] == 'tag']
    assert tag.startswith('temporal-component-part:')
    dockerfile = files['Dockerfile']
    assert dockerfile.splitlines()[1] == f'FROM {tag}'
    for path in ('/repository', '/tests', '/runner'):
        assert f'COPY --from={tag} --chown=10001:10001 {path} /parts/0{path}' in dockerfile
    # No service: the part's own command is not run as the service's.
    assert 'CMD []' in dockerfile and '/usr/local/bin/node' not in dockerfile
    run = files['run-parts.sh']
    assert '"$@"' not in run
    assert ("cd /parts/0/tests && NO_COLOR=1 PART=/parts/0/repository/frontend "
            "REPORT=/tests/reports/junit-part-0-frontend.xml CLASSNAME_PREFIX=frontend: "
            "node_modules/.bin/vitest run --config /parts/0/runner/vitest.config.mjs") in run
    assert 'bash /runner/run-parts.sh "$@"' in files['run-tests.sh']
    assert ('image', 'rm', tag) in [call[:3] for call in calls]
    # The tests are named after their part.
    assert result['image_id'] == IMAGE
    assert result['added_tests'] == [ADDED]
    assert result['changed_tests'] == [] and result['removed_tests'] == []
    published = session / 'component-tests/suite'
    assert (published / 'parts/frontend/tests/manifest.test.ts').read_text() == MANIFEST
    assert not (published / 'parts/frontend/vitest.config.mjs').exists()


async def test_the_web_clients_own_component_tests_are_frozen(session, monkeypatch):
    fake_docker(monkeypatch, {**SUITE_FILES, 'parts/frontend/tests/existing.test.ts':
                              EXISTING.replace('toBeDefined', 'toBeTruthy')}, {}, [])
    with pytest.raises(RuntimeError, match='changed or removed tests it may not touch: '
                                           'frontend:tests/existing.test.ts#creates an app'):
        await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                        CONTEXT)


async def test_a_part_whose_runner_does_not_build_is_named(session, monkeypatch):
    fake_docker(monkeypatch, SUITE_FILES, {}, [], failing='suite/tests/manifest.test.ts')
    with pytest.raises(RuntimeError, match='The component tests of part frontend could not be '
                                           'built: vitest list failed'):
        await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                        CONTEXT)
    assert not (session / 'component-tests/suite').exists()


async def test_a_seam_of_a_part_the_routing_did_not_check_is_refused(session, monkeypatch):
    fake_docker(monkeypatch, SUITE_FILES, {}, [])
    routed(session, [SERVICE])
    with pytest.raises(ApplicationError, match='names a part the repository does not have'):
        await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                        CONTEXT)


async def test_the_service_and_the_web_client_share_the_service_runners_image(
        session, monkeypatch):
    routed(session, [SERVICE, FRONTEND])
    calls: list = []
    sent: dict = {}
    fake_docker(monkeypatch, {**SUITE_FILES, 'tests/test_rooms.py': PYTHON_TEST,
                              'pytest.ini': '[pytest]\n', 'requirements.txt': 'pytest\n'},
                sent, calls)
    context = {**CONTEXT, 'routes': {
        **CONTEXT['routes'],
        'FR-2': {'route': 'component', 'reason': 'Close it.', 'seam': SERVICE_SEAM}}}
    result = await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                             context)
    assert sent['contracts/components/service/rooms.pyi'] == ROOMS_STUB
    assert sent['contracts/parts/frontend/components/src/github.d.ts'] == GITHUB_STUB
    (service_args, service), (_, part), (_, files) = builds(calls)
    # Each runner gets its own suite: the service's has no part's tests.
    assert 'suite/tests/test_rooms.py' in service and not any(
        name.startswith('suite/parts') for name in service)
    assert 'SOURCE_ROOT=src' in service_args
    assert 'suite/tests/manifest.test.ts' in part
    first, second = [call[2] for call in calls if call[0] == 'tag']
    dockerfile = files['Dockerfile']
    # Based on the service's runner, whose command runs the service's tests; the web
    # client runs with Node from its own runner.
    assert dockerfile.splitlines()[1] == f'FROM {first}'
    assert f'COPY --from={second} /usr/local/bin/node /usr/local/bin/node' in dockerfile
    # Node starts on the service's runner, with its libraries, or the build fails.
    assert ('RUN /usr/local/bin/node --version > /dev/null || { echo "The runtime of part '
            'frontend does not start') in dockerfile
    assert 'CMD' not in dockerfile
    assert '(cd /tests && "$@"); record $?' in files['run-parts.sh']
    assert sorted(result['added_tests']) == [ADDED,
                                             'tests.test_rooms#test_a_merged_room_is_closed']


def test_the_runner_exits_as_the_worst_part_did(tmp_path):
    """The generated script, run as the composite runner runs it, with parts whose
    commands exit as given."""
    language = dataclasses.replace(PYTHON, test_reports=Reports(str(tmp_path / 'r'),
                                                                    'junit*.xml'))

    def status(*codes: int) -> int:
        parts = [ComponentPart(f'p{index}', f'p{index}', TYPESCRIPT)
                 for index in range(len(codes))]
        suites = [dataclasses.replace(TYPESCRIPT.component_suite,
                                      part_command=lambda *args, code=code: f'exit {code}')
                  for code in codes]
        suite = composite(*[dataclasses.replace(part, language=dataclasses.replace(
            TYPESCRIPT, component_suite=own)) for part, own in zip(parts, suites)])
        members = list(zip(parts, suite.members))
        files = components.composite_files(language, members,
                                           {f'p{index}': 'tag' for index in range(len(codes))})
        script = tmp_path / 'run-parts.sh'
        script.write_text(files['run-parts.sh'])
        return subprocess.run(['bash', str(script)], capture_output=True).returncode

    assert status(0, 0) == 0
    assert status(1, 0) == 1 and status(0, 1) == 1
    assert status(1, 2) == 2 and status(2, 1) == 2
    assert status(137, 1) == 137 and status(1, 124) == 124 and status(3, 143) == 143


def test_a_part_report_is_named_as_the_service_language_reads_reports():
    assert components.part_report(PYTHON, 0, FRONTEND) == 'junit-part-0-frontend.xml'
    java = get_language('java')
    assert components.part_report(java, 1, ComponentPart('apps/web', 'apps/web', TYPESCRIPT)
                                  ) == 'TEST--part-1-apps-web.xml'


# ---- verification with the change ----

@pytest.fixture
def changed(session):
    write(session, {
        'implementation/service/Dockerfile': 'FROM python:3.12-slim',
        'implementation/service/src/service/rooms.py': 'CHANGED SERVICE SOURCE',
        'implementation/service/frontend/package.json': json.dumps(
            {'devDependencies': {'typescript': '5'}}),
        'implementation/service/frontend/package-lock.json': '{}',
        'implementation/service/frontend/tsconfig.json': '{}',
        'implementation/service/frontend/src/github.ts': 'CHANGED CLIENT SOURCE',
        **{f'component-tests/suite/{path}': text for path, text in SUITE_FILES.items()},
    })
    return session


async def test_the_change_is_held_to_each_parts_contract(changed, monkeypatch):
    calls: list = []
    fake_docker(monkeypatch, {}, {}, calls, failing='check.mjs')
    result = await ActivityEnvironment().run(verification.build_component_runner, 'session',
                                             'python')
    assert result['conformance_error'].startswith(
        'Part frontend: The code the component contract describes could not be installed')
    (_, check), = builds(calls)
    assert check['repository/frontend/src/github.ts'] == 'CHANGED CLIENT SOURCE'
    assert check['components/src/github.d.ts'] == GITHUB_STUB
    assert (changed / 'component-verification/parts/frontend').is_dir()

    calls.clear()
    fake_docker(monkeypatch, {}, {}, calls)
    result = await ActivityEnvironment().run(verification.build_component_runner, 'session',
                                             'python')
    assert result == {'image_id': IMAGE, 'session_id': 'session'}
    (_, check), (_, member), (_, files) = builds(calls)
    assert member['repository/frontend/src/github.ts'] == 'CHANGED CLIENT SOURCE'
    assert 'run-parts.sh' in files


def part_report(*cases: tuple[str, str | None]) -> bytes:
    """A vitest JUnit report of the web client's tests, as the composite runner writes it."""
    body = ''.join(
        f'<testcase classname="frontend:tests/manifest.test.ts" name="{name}">'
        + (f'<{outcome} message="expected"/>' if outcome else '') + '</testcase>'
        for name, outcome in cases)
    data = f'<testsuites><testsuite>{body}</testsuite></testsuites>'.encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        info = tarfile.TarInfo('./junit-part-0-frontend.xml')
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return stream.getvalue()


async def test_the_run_reports_each_parts_tests_by_their_names(changed, tmp_path, monkeypatch):
    archive = tmp_path / 'archive.tar'
    archive.write_bytes(part_report(
        ('the GitHub App manifest &gt; subscribes the app to pull_request events', 'failure')))
    executable = tmp_path / 'bin' / 'docker'
    executable.parent.mkdir()
    executable.write_text(f'#!{sys.executable}\nimport pathlib, sys\n'
                          f'sys.stdout.buffer.write(pathlib.Path({str(archive)!r}).read_bytes())\n')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(executable.parent) + os.pathsep + os.environ['PATH'])
    (changed / 'component-tests/image.json').write_text(json.dumps({'image_id': IMAGE}))

    async def docker(*args, **kwargs):
        if args[:2] == ('image', 'inspect'):
            return '10001'
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 1, 'OOMKilled': False})
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    result = await ActivityEnvironment().run(verification.run_component_suite, 'session',
                                             IMAGE, 'python', [ADDED])
    assert result['status'] == 'failed', result['error']
    assert result['tracked'] == {ADDED: {'tests': 1, 'failed': 1}}
    # The test the report has no entry of is missing, named after its part.
    assert result['missing_tests'] == ['frontend:tests/existing.test.ts#creates an app']
    # The run counts its tests by part, for the delivery page.
    assert result.get('parts') == {'frontend': {'tests': 1, 'passed': 0, 'failures': 1,
                                            'errors': 0, 'skipped': 0}}


def test_a_runs_outcomes_are_counted_by_part(tmp_path):
    (tmp_path / 'junit.xml').write_text(
        '<testsuite>'
        '<testcase classname="tests.test_rooms" name="test_a_merged_room_is_closed"/>'
        '<testcase classname="tests.test_rooms" name="test_skipped"><skipped/></testcase>'
        '<testcase classname="frontend:tests/manifest.test.ts" name="subscribes">'
        '<failure message="expected"/></testcase></testsuite>')
    suite = composite(SERVICE, FRONTEND)
    summary = verification.summarize_reports(tmp_path, Reports(str(tmp_path), 'junit*.xml'),
                                             case_id=suite.case_id, part_of=suite.part_of)
    assert summary['parts'] == {
        'service': {'tests': 2, 'passed': 1, 'failures': 0, 'errors': 0, 'skipped': 1},
        'frontend': {'tests': 1, 'passed': 0, 'failures': 1, 'errors': 0, 'skipped': 0},
    }
    # A run of the service's suite alone is not counted by part.
    assert 'parts' not in verification.summarize_reports(
        tmp_path, Reports(str(tmp_path), 'junit*.xml'))


# ---- the composite runner's script against the web client, without Docker ----

FRONTEND_DIR = Path(__file__).resolve().parents[2] / 'frontend'


@pytest.fixture(scope='module')
def composite_runner(tmp_path_factory):
    """The trees the composite runner copies from the web client's runner image, laid out
    as it lays them out, installed as that image installs them. Opt-in: it needs Node.js
    and the npm registry."""
    if os.getenv('RUN_NODE_CONTRACT_TESTS') != '1' or shutil.which('npm') is None:
        pytest.skip('set RUN_NODE_CONTRACT_TESTS=1 with Node.js and npm to run vitest')
    root = tmp_path_factory.mktemp('composite') / 'parts' / '0'
    shutil.copytree(FRONTEND_DIR, root / 'repository/frontend',
                    ignore=shutil.ignore_patterns('node_modules', 'dist'))
    contract_tests.copy_test_suite(TYPESCRIPT.component_suite, TEMPLATE, root / 'tests')
    shutil.copytree(TEMPLATE, root / 'runner',
                    ignore=lambda directory, names: [
                        name for name in names
                        if name not in TYPESCRIPT.component_suite.runner])
    for project in (root / 'repository/frontend', root / 'tests'):
        subprocess.run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'],
                       cwd=project, check=True, capture_output=True)
    return root


def test_the_composite_runner_names_the_web_clients_tests_after_it(composite_runner,
                                                                  monkeypatch):
    from tests.test_typescript_suite import MANIFEST_TEST
    write(composite_runner / 'tests', {'tests/manifest.test.ts': MANIFEST_TEST})
    reports = composite_runner.parent / 'reports'
    language = dataclasses.replace(PYTHON, test_reports=Reports(str(reports), 'junit*.xml'))
    monkeypatch.setattr(components, 'COMPOSITE_PARTS', str(composite_runner.parent))
    suite = composite(FRONTEND)
    files = components.composite_files(language, list(zip([FRONTEND], suite.members)),
                                       {'frontend': 'tag'})
    script = composite_runner.parent / 'run-parts.sh'
    script.write_text(files['run-parts.sh'])
    run = subprocess.run(['bash', str(script)], capture_output=True, text=True, timeout=300)
    # TR-6 is red against the web client as it is, and its report names the test as the
    # suite does, after its part.
    assert run.returncode == 1, run.stdout + run.stderr
    view = composite_runner.parent / 'view'
    write(view, {'parts/frontend/tests/manifest.test.ts': MANIFEST_TEST})
    expected, = suite.test_cases(view)
    tracked: dict[str, TrackedTest] = {expected: {'tests': 0, 'failed': 0}}
    summary = verification.summarize_reports(reports, language.test_reports, tracked,
                                             suite.case_id, {expected})
    assert expected == ('frontend:tests/manifest.test.ts#the GitHub App manifest > '
                        'subscribes the app to pull_request events')
    assert tracked[expected] == {'tests': 1, 'failed': 1}
    assert summary['failures'] == 1 and summary['missing_tests'] == []
