"""Work kept in the repository, waits for the provider's limits, the repository's checks
and their excerpts, decisions about further rounds of fixes, and failure causes."""

import json
import subprocess
import tarfile
from datetime import timedelta
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from temporalio.exceptions import ActivityError, ApplicationError, RetryState
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.agent_workflows import host, runtime
from app.agent_workflows.host import (
    StageInputs, StageSpec, WorkTarget, copy_regular_files, run_stage,
)
from app.agent_workflows.workflows import PROVIDER_WAIT_MAX, PROVIDER_WAIT_MIN, provider_wait
from app.delivery.causes import failure_cause, with_cause
from app.delivery.rounds import decide, outcome
from app.github import work_refs
from app.github.checkout import CHECKOUT_DIR, MANIFEST
from app.implementation.activities import has_unit_tests, is_service_file, published_files
from app.languages import get_language
from app.llm import limits
from app.verification.checks import (
    failure_excerpt, failure_signature, fallback_analysis, parse_analysis,
)

from tests.test_agent_workflows import profile, scripted_model  # noqa: F401  (fixtures)


class RateLimitError(Exception):
    status_code = 429


# The provider's limits.

def test_limit_errors_are_told_by_status_class_or_message():
    assert limits.is_limit_error(RateLimitError('slow down'))
    assert limits.is_limit_error(RuntimeError('Claude response failed: API Error: 429 '
                                              '{"type":"rate_limit_error"}'))
    assert limits.is_limit_error(RuntimeError('Claude AI usage limit reached|1700000000'))
    wrapped = RuntimeError('request failed')
    wrapped.__cause__ = RateLimitError('x')
    assert limits.is_limit_error(wrapped)
    assert not limits.is_limit_error(RuntimeError('Connection refused'))
    assert not limits.is_limit_error(ValueError('Invalid tool call'))


def test_the_wait_a_provider_asks_for_is_read_from_its_message():
    assert limits.retry_after_text('Please try again in 20s.') == 20
    assert limits.retry_after_text('Rate limit reached. Please try again in 1m30s') == 90
    assert limits.retry_after_text('usage limit reached|1700000600', now=1700000000) == 600
    assert limits.retry_after_text('Overloaded') is None
    report = limits.limit_report(RuntimeError('quota exceeded for sk-abcdefghijklmnop, '
                                              'try again in 5s'))
    assert report == {'retry_after': 5.0,
                      'message': 'quota exceeded for sk-[redacted], try again in 5s'}


def test_a_stage_waits_as_asked_within_bounds_or_doubling():
    assert provider_wait(1, None) == timedelta(minutes=5)
    assert provider_wait(2, None) == timedelta(minutes=10)
    assert provider_wait(10, None) == PROVIDER_WAIT_MAX
    assert provider_wait(1, 0) == PROVIDER_WAIT_MIN
    assert provider_wait(1, 600) == timedelta(seconds=630)
    assert provider_wait(1, 10 ** 6) == PROVIDER_WAIT_MAX


async def test_agent_stopped_by_limits_saves_where_it_was_and_goes_on_there(
        tmp_path, profile, scripted_model, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runtime, 'LIMIT_RETRY_SECONDS', 0)
    write = AIMessage(content='', tool_calls=[
        {'name': 'write_file', 'args': {'path': 'Orders.java', 'content': 'class Orders {}'},
         'id': 'orders'}])

    def refused():
        raise RateLimitError('Rate limit reached, try again in 2h')

    responses = iter([write])
    model = scripted_model([])

    async def ainvoke(messages):
        model.requests.append(list(messages))
        item = next(responses, None)
        return item if item is not None else refused()

    model.ainvoke = ainvoke
    with pytest.raises(SystemExit) as stopped:
        await runtime.run_agent(profile)
    assert stopped.value.code == limits.PROVIDER_LIMIT_EXIT
    state = json.loads((tmp_path / runtime.STATE_FILENAME).read_text())
    assert state['pending'] is None and state['turns'] == 79
    assert json.loads((tmp_path / limits.LIMIT_FILENAME).read_text()) == {
        'retry_after': 7200.0, 'message': 'Rate limit reached, try again in 2h'}
    # A new container goes on from the saved history, without answers.
    model = scripted_model([
        AIMessage(content='', tool_calls=[
            {'name': 'write_file', 'args': {'path': 'Dockerfile', 'content': 'FROM scratch'},
             'id': 'dockerfile'}]),
        AIMessage(content='Finished')])
    await runtime.run_agent(profile)
    *_, call, written = model.requests[0]
    assert call.tool_calls[0]['id'] == 'orders' and written.tool_call_id == 'orders'
    assert not (tmp_path / runtime.STATE_FILENAME).exists()
    assert not (tmp_path / limits.LIMIT_FILENAME).exists()
    assert (profile.root / 'Dockerfile').is_file()


def _stage(tmp_path, populated, work=None):
    def prepare_inputs(session_dir, context):
        def populate(workspace):
            populated.append(workspace)
            (workspace / 'notes').mkdir()
            # A file of the inputs the agent may delete, as a checkout's file.
            (workspace / 'notes/obsolete.md').write_text('# Obsolete')
        return StageInputs(context={}, populate=populate)

    return StageSpec(
        role='documentation', artifact_subdir='docs-stage', workspace_name='notes',
        output_subdir='published', heartbeat='Authoring docs', image_label=None,
        image_build=docker_agents.AgentImageBuild(tmp_path, tmp_path / 'Dockerfile'),
        prepare_inputs=prepare_inputs,
        sanitize_output=lambda source, target: copy_regular_files(
            source, target, include=lambda relative: True),
        validate_output=lambda clean: None, work=work)


async def test_stage_stopped_by_limits_keeps_its_workspace_and_goes_on_from_it(
        tmp_path, monkeypatch):
    populated: list[Path] = []
    saved: list[tuple[str, bool]] = []

    async def save(stage, session_dir, context, tree, status, published=False):
        saved.append((status, published))

    monkeypatch.setattr(host, 'save_stage_work', save)
    sent: list[dict[str, bytes]] = []
    exit_codes = iter([limits.PROVIDER_LIMIT_EXIT, 0])

    async def docker(*args, **kwargs):
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.append({item.name: archive.extractfile(item).read()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': next(exit_codes)})
        if args[0] == 'cp' and args[1].endswith(':/workspace/.'):
            (Path(args[2]) / 'notes').mkdir()
            (Path(args[2]) / 'notes/draft.md').write_text('# Draft')
            (Path(args[2]) / runtime.STATE_FILENAME).write_text(json.dumps({'pending': None}))
            (Path(args[2]) / limits.LIMIT_FILENAME).write_text(
                json.dumps({'retry_after': 60, 'message': 'rate limit'}))
        if args[0] == 'cp' and args[1].endswith(':/workspace/notes/.'):
            (Path(args[2]) / 'index.md').write_text('# Published')
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    stage = _stage(tmp_path, populated)
    environment = ActivityEnvironment()
    result = await environment.run(run_stage, stage, 'session', {}, tmp_path)
    assert result == {'session_id': 'session',
                      'provider_limit': {'retry_after': 60.0, 'message': 'rate limit'}}
    result = await environment.run(run_stage, stage, 'session', {host.RESUME_KEY: True},
                                   tmp_path)
    assert len(populated) == 1
    assert sent[1]['notes/draft.md'] == b'# Draft'
    assert runtime.ANSWERS_FILENAME not in sent[1]
    assert (Path(result['path']) / 'index.md').read_text() == '# Published'
    # What the stopped agent wrote was saved, and so was the published output.
    assert saved == [("stopped by the provider's limits", False), ('built', True)]


async def test_failed_agent_output_is_saved_before_the_failure_is_raised(tmp_path, monkeypatch):
    saved: list[tuple[str, set[str]]] = []

    async def save(stage, session_dir, context, tree, status, published=False):
        saved.append((status, {item.name for item in tree.iterdir()}))

    monkeypatch.setattr(host, 'save_stage_work', save)

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 1})
        if args[0] == 'logs':
            return 'Traceback\nRuntimeError: the model gave up'
        if args[0] == 'cp' and args[1].endswith(':/workspace/notes/.'):
            (Path(args[2]) / 'half.md').write_text('# Half done')
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    with pytest.raises(RuntimeError, match='exited with code 1'):
        await ActivityEnvironment().run(run_stage, _stage(tmp_path, []), 'session', {}, tmp_path)
    assert saved == [('agent failed: Documentation agent exited with code 1. Log: '
                      f'{tmp_path}/session/docs-stage/agent.log', {'half.md'})]


# Work refs.

def _git(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def session(tmp_path):
    """A session whose checkout is a clone of a local origin, with its manifest."""
    origin = tmp_path / 'origin.git'
    seed = tmp_path / 'seed'
    seed.mkdir()
    _git('init', '-q', '-b', 'main', cwd=seed)
    (seed / 'app.py').write_text('print(1)\n')
    (seed / 'old.py').write_text('gone\n')
    (seed / 'contract-tests').mkdir()
    (seed / 'contract-tests/test_api.py').write_text('def test_api(): pass\n')
    _git('add', '-A', cwd=seed)
    _git('-c', 'user.name=t', '-c', 'user.email=t@e', 'commit', '-qm', 'base', cwd=seed)
    _git('clone', '-q', '--bare', str(seed), str(origin), cwd=tmp_path)
    session_dir = tmp_path / 'generated' / 'api-design-session-abc'
    session_dir.mkdir(parents=True)
    _git('clone', '-q', str(origin), str(session_dir / CHECKOUT_DIR), cwd=tmp_path)
    commit = _git('rev-parse', 'HEAD', cwd=session_dir / CHECKOUT_DIR)
    (session_dir / MANIFEST).write_text(json.dumps(
        {'full_name': 'octocat/project', 'branch': 'main', 'commit': commit}))
    return session_dir, origin, commit


async def _token():
    return 'secret', 'app[bot]\n1+app[bot]@users.noreply.github.com'


async def test_work_is_committed_to_hidden_refs_on_the_base_commit(session, tmp_path):
    session_dir, origin, base = session
    tree = tmp_path / 'service'
    tree.mkdir()
    (tree / 'app.py').write_text('print(2)\n')
    (tree / 'new.py').write_text('new\n')
    messages = []

    async def write_message(stage, status, diff):
        messages.append((stage, status, diff))
        return 'Change the printed number\n\nIt prints 2 now.'

    include = lambda relative: relative.parts[0] != 'contract-tests'  # noqa: E731
    commit = await work_refs.save_work(
        session_dir, session_dir.name, 'implementation', tree, include, status='built',
        published=True, token=_token, write_message=write_message)
    ref = f'refs/sdlc/{session_dir.name}/implementation'
    assert _git('rev-parse', f'{ref}/latest', cwd=origin) == commit
    assert _git('rev-parse', f'{ref}/published', cwd=origin) == commit
    assert _git('rev-parse', f'{commit}^', cwd=origin) == base
    files = set(_git('ls-tree', '-r', '--name-only', commit, cwd=origin).splitlines())
    # The author removed old.py; the contract tests it never saw stay.
    assert files == {'app.py', 'new.py', 'contract-tests/test_api.py'}
    message = _git('log', '-1', '--format=%B', commit, cwd=origin)
    assert message.startswith('Change the printed number\n\nIt prints 2 now.')
    assert 'Sdlc-Status: built' in message
    assert 'app[bot]' in _git('log', '-1', '--format=%an', commit, cwd=origin)
    assert messages[0][:2] == ('implementation', 'built') and 'new.py' in messages[0][2]
    # The checkout itself is left as it was.
    assert _git('status', '--porcelain', cwd=session_dir / CHECKOUT_DIR) == ''

    # The next save goes on from it; without the LLM the message is composed.
    (tree / 'app.py').write_text('print(3)\n')

    async def failing(stage, status, diff):
        raise RuntimeError('provider down')

    second = await work_refs.save_work(
        session_dir, session_dir.name, 'implementation', tree, include,
        status='agent failed: boom', token=_token, write_message=failing)
    assert _git('rev-parse', f'{second}^', cwd=origin) == commit
    assert _git('rev-parse', f'{ref}/published', cwd=origin) == commit
    assert _git('log', '-1', '--format=%s', second, cwd=origin) == (
        'WIP implementation: agent failed: boom (app.py)')

    # A worker that lost the published output restores it from the published ref.
    target = tmp_path / 'restored'
    assert await work_refs.restore_work(session_dir, session_dir.name, 'implementation',
                                        target, include, token=_token)
    assert {item.name for item in target.iterdir()} == {'app.py', 'new.py'}
    assert (target / 'app.py').read_text() == 'print(2)\n'


async def test_work_is_saved_without_files_too_large_to_push(session, tmp_path, monkeypatch):
    session_dir, origin, _ = session
    monkeypatch.setattr(work_refs, 'MAX_FILE_BYTES', 100)
    tree = tmp_path / 'service'
    tree.mkdir()
    (tree / 'app.py').write_text('print(2)\n')
    (tree / 'core').write_bytes(b'\0' * 101)
    commit = await work_refs.save_work(
        session_dir, session_dir.name, 'implementation', tree, lambda _: True,
        status='agent failed: boom', token=_token)
    assert _git('ls-tree', '-r', '--name-only', commit, cwd=origin) == 'app.py'


async def test_work_of_a_session_without_a_repository_is_not_saved(tmp_path):
    tree = tmp_path / 'tree'
    tree.mkdir()
    assert await work_refs.save_work(tmp_path, 'session', 'tests', tree, lambda _: True,
                                     status='built', token=_token) is None


async def test_lost_snapshot_is_taken_again_from_the_base_commit(session, tmp_path):
    session_dir, _, _ = session
    checkout = session_dir / CHECKOUT_DIR
    assert not await work_refs.checkout_moved(session_dir)
    (checkout / 'contract-tests/test_api.py').write_text('def test_changed(): pass\n')
    assert await work_refs.checkout_moved(session_dir)
    base = tmp_path / 'base'
    assert await work_refs.base_files(session_dir, base)
    assert (base / 'contract-tests/test_api.py').read_text() == 'def test_api(): pass\n'


# The implementation author's unit tests.

def test_unit_tests_are_published_only_from_a_tree_that_carries_them(tmp_path):
    python = get_language('python')
    assert not is_service_file(python, Path('backend/tests/test_x.py'))
    assert is_service_file(python, Path('backend/tests/test_x.py'), unit_tests=True)
    assert not is_service_file(python, Path('contract-tests/tests/test_x.py'), unit_tests=True)
    assert not is_service_file(python, Path('component-tests/test_x.py'), unit_tests=True)
    tree = tmp_path / 'service'
    (tree / 'backend').mkdir(parents=True)
    (tree / 'backend/app.py').write_text('')
    # A tree published before authors maintained unit tests never removes them.
    assert not has_unit_tests(python, tree)
    assert not published_files(python, tree)(Path('backend/tests/test_x.py'))
    (tree / 'backend/tests').mkdir()
    (tree / 'backend/tests/test_x.py').write_text('')
    assert has_unit_tests(python, tree)
    assert published_files(python, tree)(Path('backend/tests/test_x.py'))


# The repository's checks.

PYTEST_LOG = '''Collecting httpx==0.27
  Downloading httpx-0.27.0-py3-none-any.whl (75 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 75.6/75.6 kB 3.1 MB/s eta 0:00:00
Requirement already satisfied: anyio in /usr/lib/python3/dist-packages
Successfully installed httpx-0.27.0
============================= test session starts ==============================
tests/test_ok.py ......                                                  [ 40%]
tests/test_rooms.py ..FF                                                 [100%]
=================================== FAILURES ===================================
________________________________ test_list_rooms _______________________________
    def test_list_rooms():
>       assert rooms() == [1]
E       AssertionError: assert [] == [1]
tests/test_rooms.py:4: AssertionError
________________________________ test_get_room _________________________________
    def test_get_room():
>       assert rooms() == [1]
E       AssertionError: assert [] == [1]
tests/test_rooms.py:8: AssertionError
----------------------------- Captured stdout call -----------------------------
debug output nobody needs
=========================== short test summary info ============================
FAILED tests/test_rooms.py::test_list_rooms - AssertionError: assert [] == [1]
FAILED tests/test_rooms.py::test_get_room - AssertionError: assert [] == [1]
========================= 2 failed, 6 passed in 0.12s ==========================
'''


def test_failure_excerpt_keeps_each_failure_once_without_the_noise():
    excerpt = failure_excerpt(PYTEST_LOG)
    for noise in ('Downloading', 'Requirement already', 'Successfully installed', '━',
                  'debug output', 'tests/test_ok.py'):
        assert noise not in excerpt
    # The same failure of two tests is shown once; the summary names both tests.
    assert excerpt.count('E       AssertionError: assert [] == [1]') == 1
    assert 'test_list_rooms' in excerpt and 'FAILED tests/test_rooms.py::test_get_room' in excerpt
    assert '2 failed, 6 passed' in excerpt
    assert failure_signature(excerpt) == ['E       AssertionError: assert [] == [#]',
                                          'AssertionError: assert [] == [#]']


def test_failure_excerpt_reads_other_toolchains():
    log = '\n'.join([
        '[INFO] Downloading from central: https://repo/x.pom',
        '[INFO] Tests run: 4, Failures: 0, Errors: 0, Skipped: 0',
        '[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0',
        '[ERROR] RoomsTest.lists:12 expected: <1> but was: <0>',
        'npm WARN deprecated inflight@1.0.6',
        'src/App.tsx(3,7): error TS2322: Type string is not assignable to type number.',
    ] + ['progress line'] * 100)
    excerpt = failure_excerpt(log)
    assert 'Downloading' not in excerpt and 'npm WARN' not in excerpt
    assert 'RoomsTest.lists:12 expected: <1> but was: <0>' in excerpt
    assert 'error TS2322' in excerpt
    assert '(the line above repeated 99 more times)' in excerpt
    assert len(failure_excerpt('x\n' * 100000, limit=500)) <= 500


def test_analysis_is_parsed_or_falls_back_to_what_the_output_says():
    assert parse_analysis('```json\n{"summary": "s", "files": ["a.py"], '
                          '"instructions": "Fix a.py"}\n```') == {
        'cause': 'code', 'summary': 's', 'files': ['a.py'], 'instructions': 'Fix a.py',
        'analysed': True}
    assert parse_analysis('{"cause": "environment", "summary": "git is not installed", '
                          '"instructions": "None"}')['cause'] == 'environment'
    assert parse_analysis('{"cause": "weather", "instructions": "x"}')['cause'] == 'code'
    assert parse_analysis('no JSON here') is None
    fallback = fallback_analysis('noise\nE   KeyError: x\nmore')
    assert fallback['summary'] == 'E   KeyError: x' and not fallback['analysed']
    assert fallback['cause'] == 'code'


# Further rounds of fixes.

def test_rounds_go_on_while_they_make_progress():
    a, b, c = (outcome(n, {f'test {i}' for i in range(n)}) for n in (3, 2, 1))
    assert decide([a], 12)['go']
    assert decide([a, b, c], 12)['go']
    # Other failures than before are progress too.
    assert decide([a, outcome(3, {'other'})], 12)['go']


def test_rounds_stop_when_they_go_round_in_circles_or_make_things_worse():
    same = outcome(2, {'x', 'y'})
    assert decide([same, same, same], 12)['go']
    stopped = decide([same, same, same, same], 12)
    assert not stopped['go'] and 'same failures' in stopped['reason']
    worse = decide([outcome(1, {'a'}), outcome(2, {'a', 'b'}), outcome(3, {'a', 'b', 'c'})], 12)
    assert not worse['go'] and 'grew' in worse['reason']
    flat = [outcome(2, {'a', 'b'}), outcome(3, {'c'}), outcome(2, {'d', 'e'}),
            outcome(3, {'f'}), outcome(2, {'d', 'e'})]
    assert decide(flat, 12)['go'] is True
    assert not decide([outcome(1, {'a'})] + [outcome(5, {f'b{i}'}) for i in range(4)],
                      12)['go']
    ceiling = decide([outcome(5 - i, {str(i)}) for i in range(4)], 3)
    assert not ceiling['go'] and '3 rounds' in ceiling['reason']
    assert not decide([outcome(0, set())], 12)['go']


# Failure causes.

def test_failures_name_their_cause_without_credentials():
    inner = ApplicationError('Test-generation agent exited with code 1. Log: /data/agent.log\n'
                             'INFO starting\nopenai.AuthenticationError: invalid api_key=sk-12345678abcd')
    activity = ActivityError('Activity task failed', scheduled_event_id=1, started_event_id=2,
                             identity='w', activity_type='a', activity_id='1',
                             retry_state=RetryState.NON_RETRYABLE_FAILURE)
    activity.__cause__ = inner
    cause = failure_cause(activity)
    assert cause.startswith('Test-generation agent exited with code 1.')
    assert 'AuthenticationError' in cause and 'sk-1234' not in cause
    assert with_cause('The tests could not be written.', activity).startswith(
        'The tests could not be written. Cause: Test-generation agent exited')
    assert with_cause('Nothing.', None) == 'Nothing.'


async def test_stage_whose_stopped_workspace_was_lost_starts_over_from_its_saved_work(
        tmp_path, monkeypatch):
    populated: list[Path] = []
    restored: list[str] = []

    async def save(stage, session_dir, context, tree, status, published=False):
        pass

    async def restore_work(session_dir, session_id, stage, target, include, *, prefix='',
                           token, kind=work_refs.PUBLISHED):
        if kind != work_refs.LATEST:
            return False
        restored.append(stage)
        target.mkdir(parents=True)
        (target / 'draft.md').write_text('# Saved when the provider stopped it')
        return True

    monkeypatch.setattr(host, 'save_stage_work', save)
    monkeypatch.setattr(work_refs, 'restore_work', restore_work)
    sent: list[dict[str, bytes]] = []

    async def docker(*args, **kwargs):
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.append({item.name: archive.extractfile(item).read()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1].endswith(':/workspace/notes/.'):
            (Path(args[2]) / 'index.md').write_text('# Published')
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    # The work covers Markdown files only; anything else of the project is the inputs'.
    stage = _stage(tmp_path, populated, work=lambda context: WorkTarget(
        'docs', lambda relative: relative.suffix == '.md'))
    # No paused workspace on this worker's disk: the run is not refused.
    result = await ActivityEnvironment().run(run_stage, stage, 'session',
                                             {host.RESUME_KEY: True}, tmp_path)
    assert len(populated) == 1 and restored == ['docs']
    assert sent[0]['notes/draft.md'] == b'# Saved when the provider stopped it'
    # The stopped agent had deleted it: the saved work has it no more, nor the project.
    assert 'notes/obsolete.md' not in sent[0]
    assert 'input.json' in sent[0]
    assert (Path(result['path']) / 'index.md').read_text() == '# Published'


async def test_checks_output_is_kept_to_its_end_within_a_byte_bound(tmp_path, monkeypatch):
    fake = tmp_path / 'bin'
    fake.mkdir()
    # One huge line and the end of the run: no line count bounds it.
    (fake / 'docker').write_text('#!/bin/sh\nhead -c 300000 /dev/zero | tr "\\0" x\n'
                                 'printf "\\nE   AssertionError: the end\\n"\n')
    (fake / 'docker').chmod(0o755)
    monkeypatch.setenv('PATH', f'{fake}:/usr/bin:/bin')
    monkeypatch.setattr(docker_agents, 'CHECKS_LOG_BYTES', 1000)
    log = tmp_path / 'output.log'
    await docker_agents._save_logs('checks-container', log)
    text = log.read_text()
    assert len(text) < 1100
    assert text.startswith('… (the start of the output was cut)')
    assert text.rstrip().endswith('E   AssertionError: the end')
