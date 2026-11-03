import json
import tarfile
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.api_design.models import empty_api
from app.diagnosis import activities
from app.languages import OTHER_PARTS_BUILD_DIRS

RUN = 'f' * 32
BROKEN = {'class_name': 'OrdersTest', 'name': 'creates'}
VERDICT = {'target': 'tests', 'summary': 'The contract documents 201.',
           # Only a test that failed can be found broken; anything else named is dropped.
           'tests': [BROKEN, {'class_name': 'OrdersTest', 'name': 'lists'}, 'creates'],
           'instructions': 'POST /orders returns 201; assert 201, not 200.'}


def failed_verification(session_dir: Path, **changes) -> dict:
    return {'status': 'failed', 'tests': 2, 'passed': 1, 'failures': 1, 'errors': 0, 'skipped': 0,
            'test_failures': [{'class_name': 'OrdersTest', 'name': 'creates', 'kind': 'failure',
                               'message': 'expected 200 but was 201' + 'x' * 5000}],
            'service_log': 'service log', 'runner_log': 'runner log',
            'path': str(session_dir / 'verification' / RUN), **changes}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    session_dir = tmp_path / 'session'
    for path, text in {
        'implementation/service/src/main/java/Orders.java': 'IMPLEMENTATION SOURCE',
        'tests/suite/src/test/java/OrdersTest.java': 'TEST SOURCE',
        f'verification/{RUN}/reports/TEST-OrdersTest.xml': '<testsuite/>',
        f'verification/{RUN}/service_log.log': 'service log',
        'verification/' + 'e' * 32 + '/reports/TEST-Old.xml': 'EARLIER RUN',
        'implementation/agent.log': 'UNRELATED AGENT LOG',
    }.items():
        (session_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (session_dir / path).write_text(text)
    return session_dir


def fake_docker(monkeypatch, verdict, sent: dict):
    """Stands in for the Docker CLI and the external agent's filesystem only."""
    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/review/.'):
            output = Path(args[2])
            if verdict == 'link':
                (output / 'verdict.json').symlink_to('/etc/passwd')
            elif verdict is not None:
                (output / 'verdict.json').write_text(
                    verdict if isinstance(verdict, str) else json.dumps(verdict))
        if args[0] == 'rm':
            sent['removed'] = True
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)


async def test_diagnosis_reviews_both_sides_and_publishes_a_bounded_verdict(session, monkeypatch):
    sent: dict = {}
    fake_docker(monkeypatch, {**VERDICT, 'summary': 's' * 5000, 'extra': 'ignored'}, sent)
    context = {'requirements': [], 'requirements_document': '# Requirements', 'language': 'java',
               'api_design': empty_api(), 'verification': failed_verification(session)}
    result = await ActivityEnvironment().run(activities.diagnose_failure, 'session', context)
    assert result == {'session_id': 'session', 'target': 'tests', 'summary': 's' * 1000,
                      'tests': [BROKEN],
                      'instructions': VERDICT['instructions'], 'path': str(session / 'diagnosis')}
    assert json.loads((session / 'diagnosis/verdict.json').read_text()) == result
    removed = sent.pop('removed')
    payload = json.loads(sent.pop('input.json'))
    sent.pop('llm-settings.json')  # Shared model input is covered by the adapter lifecycle tests.
    assert json.loads(sent.pop('language.json')) == {
        'name': 'java', 'build_dirs': ['target', *OTHER_PARTS_BUILD_DIRS], 'label': 'Java (Spring Boot)', 'report_pattern': 'TEST-*.xml'}
    # The reviewer gets both source trees and this run's evidence, and nothing else.
    assert sent == {'review/service/src/main/java/Orders.java': 'IMPLEMENTATION SOURCE',
                    'review/tests/src/test/java/OrdersTest.java': 'TEST SOURCE',
                    'review/verification/reports/TEST-OrdersTest.xml': '<testsuite/>',
                    'review/verification/service_log.log': 'service log'}
    assert payload['requirements_document'] == '# Requirements' and payload['api_design']
    failure = payload['verification']['test_failures'][0]
    assert failure['name'] == 'creates' and len(failure['message']) == 600
    assert payload['verification']['failing_tests'] == 1
    assert removed


@pytest.mark.parametrize('verdict', [
    None, 'link', 'not json', '[]', {**VERDICT, 'target': 'both'}, {**VERDICT, 'instructions': ' '},
    {**VERDICT, 'tests': []}, {**VERDICT, 'tests': [{'class_name': 'OrdersTest', 'name': 'lists'}]},
])
async def test_unusable_verdicts_never_select_a_repair(session, monkeypatch, verdict):
    sent: dict = {}
    fake_docker(monkeypatch, verdict, sent)
    context = {'api_design': empty_api(), 'language': 'java', 'verification': failed_verification(session)}
    with pytest.raises(RuntimeError, match='[Vv]erdict'):
        await ActivityEnvironment().run(activities.diagnose_failure, 'session', context)
    assert not (session / 'diagnosis/verdict.json').exists()
    assert sent['removed']


@pytest.mark.parametrize('changes', [
    {'status': 'passed'}, {'path': '/etc'}, {'path': '../../other-session/verification/' + RUN},
    {'path': 'a' * 32},
])
async def test_diagnosis_requires_this_sessions_failed_run(session, monkeypatch, changes):
    sent: dict = {}
    fake_docker(monkeypatch, VERDICT, sent)
    (session.parent / 'other-session/verification' / RUN).mkdir(parents=True)
    context = {'api_design': empty_api(), 'language': 'java', 'verification': failed_verification(session, **changes)}
    if changes.get('path', '').startswith('../'):
        # Only the run name is honoured, so a foreign path resolves inside this session.
        await ActivityEnvironment().run(activities.diagnose_failure, 'session', context)
        assert 'review/verification/service_log.log' in sent
        return
    with pytest.raises(ApplicationError, match='artifacts are unavailable'):
        await ActivityEnvironment().run(activities.diagnose_failure, 'session', context)
    assert not sent


COMPONENT_RUN = 'c' * 32
COMPONENT_BROKEN = {'class_name': 'tests.test_rooms', 'name': 'test_closes'}


def failed_component_run(session_dir: Path, **changes) -> dict:
    return {'status': 'failed', 'tests': 3, 'passed': 2, 'failures': 1, 'errors': 0,
            'skipped': 0, 'service_log': '', 'runner_log': 'runner log',
            'test_failures': [{**COMPONENT_BROKEN, 'kind': 'failure', 'message': 'assert False'}],
            'path': str(session_dir / 'component-runs' / COMPONENT_RUN), **changes}


@pytest.fixture
def component_session(tmp_path, monkeypatch):
    """A Python change with contract and component tests, as verification leaves it."""
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    session_dir = tmp_path / 'session'
    for path, text in {
        'implementation/service/app/rooms.py': 'IMPLEMENTATION SOURCE',
        'tests/suite/tests/test_http.py': 'CONTRACT TEST SOURCE',
        'repository-tests/suite/tests/test_http.py': 'REPOSITORY TEST SOURCE',
        'component-tests/suite/tests/test_rooms.py': 'COMPONENT TEST SOURCE',
        'routing/components/app/rooms.pyi': 'STUB',
        f'verification/{RUN}/reports/TEST-http.xml': '<testsuite/>',
        f'component-runs/{COMPONENT_RUN}/reports/TEST-rooms.xml': '<testsuite name="rooms"/>',
        f'component-runs/{COMPONENT_RUN}/runner_log.log': 'runner log',
        f'component-runs/{COMPONENT_RUN}/result.json': '{}',
    }.items():
        (session_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (session_dir / path).write_text(text)
    return session_dir


def component_context(session_dir: Path, contract: str | None = 'failed',
                      suite: str = 'tests') -> dict:
    verification = None if contract is None else failed_verification(session_dir, **(
        {} if contract == 'failed' else {'status': 'passed', 'failures': 0,
                                         'test_failures': []}))
    return {'api_design': empty_api(), 'language': 'python', 'brief': {'requirements': []},
            'component_routes': {'FR-2': {'route': 'component', 'reason': 'R.'}},
            'verification': verification, 'contract_suite': suite,
            'component_verification': failed_component_run(session_dir)}


async def test_a_change_with_component_tests_is_diagnosed_on_both_suites(
        component_session, monkeypatch):
    sent: dict = {}
    verdict = {**VERDICT, 'suite': 'component', 'tests': [COMPONENT_BROKEN, BROKEN]}
    fake_docker(monkeypatch, verdict, sent)
    result = await ActivityEnvironment().run(activities.diagnose_failure, 'session',
                                             component_context(component_session))
    # The broken tests are those of the suite the verdict names that failed.
    assert result['target'] == 'tests' and result['suite'] == 'component'
    assert result['tests'] == [COMPONENT_BROKEN]
    payload = json.loads(sent['input.json'])
    assert payload['verification']['status'] == 'failed'
    assert payload['component_verification']['test_failures'][0]['name'] == 'test_closes'
    assert payload['component_routes'] and 'contract_suite' not in payload
    # Both suites, their failed runs and the stubs; never the session's other artifacts.
    review = {name: text for name, text in sent.items() if name.startswith('review/')}
    assert review == {
        'review/service/app/rooms.py': 'IMPLEMENTATION SOURCE',
        'review/tests/tests/test_http.py': 'CONTRACT TEST SOURCE',
        'review/verification/reports/TEST-http.xml': '<testsuite/>',
        'review/component-tests/tests/test_rooms.py': 'COMPONENT TEST SOURCE',
        'review/component-contract/app/rooms.pyi': 'STUB',
        'review/component-verification/reports/TEST-rooms.xml': '<testsuite name="rooms"/>',
        'review/component-verification/runner_log.log': 'runner log'}


@pytest.mark.parametrize('contract, verdict, error', [
    # Both suites failed: the verdict has to say whose tests are wrong.
    ('failed', {**VERDICT, 'tests': [COMPONENT_BROKEN]}, 'does not name the contract or'),
    # Only the component tests failed: the contract tests cannot be at fault.
    ('passed', {**VERDICT, 'suite': 'contract'}, 'which did not fail'),
    ('passed', {**VERDICT, 'tests': [BROKEN]}, 'names no failing test as broken'),
])
async def test_a_verdict_against_the_tests_names_a_suite_that_failed(
        component_session, monkeypatch, contract, verdict, error):
    fake_docker(monkeypatch, verdict, {})
    with pytest.raises(RuntimeError, match=error):
        await ActivityEnvironment().run(activities.diagnose_failure, 'session',
                                        component_context(component_session, contract))


async def test_the_only_failed_suite_is_the_one_condemned(component_session, monkeypatch):
    fake_docker(monkeypatch, {**VERDICT, 'tests': [COMPONENT_BROKEN]}, {})
    result = await ActivityEnvironment().run(activities.diagnose_failure, 'session',
                                             component_context(component_session, None))
    assert result['suite'] == 'component' and result['tests'] == [COMPONENT_BROKEN]


async def test_the_repositorys_own_contract_tests_are_never_condemned(
        component_session, monkeypatch):
    sent: dict = {}
    fake_docker(monkeypatch, {**VERDICT, 'suite': 'contract'}, sent)
    context = component_context(component_session, suite='repository-tests')
    with pytest.raises(RuntimeError, match="repository's own contract tests"):
        await ActivityEnvironment().run(activities.diagnose_failure, 'session', context)
    assert sent['review/tests/tests/test_http.py'] == 'REPOSITORY TEST SOURCE'
    assert json.loads(sent['input.json'])['contract_suite'] == 'repository'
    # The fix is the one to blame, then.
    fake_docker(monkeypatch, {**VERDICT, 'target': 'implementation'}, {})
    result = await ActivityEnvironment().run(activities.diagnose_failure, 'session', context)
    assert result['target'] == 'implementation' and 'suite' not in result


def test_the_diagnosis_is_told_about_the_component_tests(tmp_path):
    from app.diagnosis import agent
    language = {'label': 'Python', 'report_pattern': '*.xml'}
    assert 'component-tests/' not in agent.prompt({}, language)
    text = agent.prompt({'component_verification': {}, 'contract_suite': 'repository'}, language)
    assert '"suite": "contract" or "component"' in text and agent.REPOSITORY_SUITE_PROMPT in text
    # Its agent does not finish until a verdict against the tests names their suite.
    (tmp_path / 'verdict.json').write_text(json.dumps(VERDICT))
    agent.validate_completion(False, tmp_path)
    with pytest.raises(RuntimeError, match='does not name their suite'):
        agent.validate_completion(True, tmp_path)
    (tmp_path / 'verdict.json').write_text(json.dumps({**VERDICT, 'suite': 'component'}))
    agent.validate_completion(True, tmp_path)
