import json
import tarfile
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.api_design.models import empty_api
from app.diagnosis import activities

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
        'name': 'java', 'build_dirs': ['target'], 'label': 'Java (Spring Boot)', 'report_pattern': 'TEST-*.xml'}
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
