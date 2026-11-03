"""Routing a change's criteria to the suite that can prove them, and the test author's
finding that a criterion has none."""
import json
import tarfile
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.api_design.models import empty_api
from app.routing import activities
from app.routing.models import judge_routes
from app.test_generation.activities import read_unobservable

REQUIREMENT = {'category': 'functional', 'title': 'T', 'description': 'D', 'priority': 'must'}
BRIEF = {'requirements': [{**REQUIREMENT, 'id': 'FR-2'}, {**REQUIREMENT, 'id': 'FR-3'}],
         'requirements_document': '# Brief', 'retired': []}
CONTEXT = {'requirements': [], 'requirements_document': '# Contract', 'language': 'java',
           'api_design': empty_api(), 'brief': BRIEF,
           'repository': {'full_name': 'octo/hotel'}}
ROUTES = {'routes': [
    {'requirement_id': 'FR-2', 'route': 'contract', 'reason': 'POST /rooms, then GET.'},
    {'requirement_id': 'FR-3', 'route': 'unobservable', 'reason': '  Needs a merged\n pull request. '},
]}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    session_dir = tmp_path / 'session'
    for path, text in {
        'repository/Dockerfile': 'FROM eclipse-temurin',
        'repository/src/main/java/Rooms.java': 'SERVICE SOURCE',
        'repository/contract-tests/README.md': 'How the suite runs',
        'repository/.git/config': 'GIT DATA',
        'repository/target/rooms.jar': 'BUILD OUTPUT',
        'tests/suite/RoomsTest.java': 'NOT THE REPOSITORY',
    }.items():
        (session_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (session_dir / path).write_text(text)
    return session_dir


def fake_docker(monkeypatch, routes, sent: dict):
    """Stands in for the Docker CLI and the external agent's filesystem only."""
    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/routing/.') and routes is not None:
            (Path(args[2]) / 'routes.json').write_text(
                routes if isinstance(routes, str) else json.dumps(routes))
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)


async def test_routing_reads_the_repository_and_returns_one_route_per_criterion(session, monkeypatch):
    sent: dict = {}
    fake_docker(monkeypatch, ROUTES, sent)
    result = await ActivityEnvironment().run(activities.route_requirements, 'session', CONTEXT)
    assert result == {'session_id': 'session', 'path': str(session / 'routing'), 'routes': {
        'FR-2': {'route': 'contract', 'reason': 'POST /rooms, then GET.'},
        'FR-3': {'route': 'unobservable', 'reason': 'Needs a merged pull request.'}}}
    assert json.loads((session / 'routing/routes.json').read_text()) == result
    payload = json.loads(sent.pop('input.json'))
    assert payload['brief'] == BRIEF and payload['api_design']
    assert 'repository' not in payload and 'language' not in payload
    # The router sees the repository as it is, without its Git data or build output, and
    # nothing the delivery's authors wrote.
    assert {name: text for name, text in sent.items() if name.startswith('routing/')} == {
        'routing/repository/Dockerfile': 'FROM eclipse-temurin',
        'routing/repository/src/main/java/Rooms.java': 'SERVICE SOURCE',
        'routing/repository/contract-tests/README.md': 'How the suite runs'}


@pytest.mark.parametrize('routes, error', [
    (None, 'did not produce routes'),
    ('not json', 'not valid JSON'),
    ([], 'not a list of routes'),
    ({'routes': ROUTES['routes'][:1]}, 'without a route: FR-3'),
    ({'routes': [*ROUTES['routes'], ROUTES['routes'][0]]}, 'routed twice'),
    ({'routes': [*ROUTES['routes'], {**ROUTES['routes'][0], 'requirement_id': 'FR-9'}]},
     'does not have: FR-9'),
    ({'routes': [ROUTES['routes'][0], {**ROUTES['routes'][1], 'route': 'component'}]},
     'no valid route'),
    ({'routes': [ROUTES['routes'][0], {**ROUTES['routes'][1], 'reason': ' '}]}, 'no reason'),
])
async def test_an_unusable_routing_routes_nothing(session, monkeypatch, routes, error):
    fake_docker(monkeypatch, routes, {})
    with pytest.raises(RuntimeError, match=error):
        await ActivityEnvironment().run(activities.route_requirements, 'session', CONTEXT)
    assert not (session / 'routing/routes.json').exists()


async def test_routing_needs_the_checkout_and_a_brief(session, monkeypatch):
    fake_docker(monkeypatch, ROUTES, {})
    with pytest.raises(ApplicationError, match='no requirements'):
        await ActivityEnvironment().run(activities.route_requirements, 'session',
                                        {**CONTEXT, 'brief': {**BRIEF, 'requirements': []}})
    with pytest.raises(ApplicationError, match='checkout is unavailable'):
        await ActivityEnvironment().run(activities.route_requirements, 'other', CONTEXT)


def test_unobservable_criteria_are_named_with_their_reasons():
    routes = {f'FR-{number}': {'route': 'unobservable', 'reason': f'R{number}.',
                               'source': 'routing'} for number in range(2, 9)}
    routes['FR-1'] = {'route': 'contract', 'reason': 'GET /rooms.', 'source': 'routing'}
    status, error = judge_routes(routes)
    assert status == 'unobservable'
    assert error.startswith('The contract tests cannot observe 7 of the 8 acceptance criteria')
    assert 'FR-2: R2.; FR-3: R3.' in error and 'FR-6: R6.; 2 more' in error
    assert 'FR-7' not in error
    assert judge_routes({'FR-1': routes['FR-1']}) == ('routed', None)


def test_the_test_author_may_name_criteria_of_the_brief_instead_of_writing_a_suite(tmp_path):
    ids = ['FR-2', 'FR-3']
    # A suite without the file is published as usual.
    assert read_unobservable(ids, tmp_path) is None
    file = tmp_path / 'unobservable.json'
    file.write_text(json.dumps({'unobservable': [
        {'requirement_id': 'FR-3', 'reason': 'Needs a pull request.'}]}))
    assert read_unobservable(ids, tmp_path) == {'unobservable': [
        {'requirement_id': 'FR-3', 'reason': 'Needs a pull request.'}]}
    for content, error in [
        ('[]', 'names no criterion'), ('{"unobservable": []}', 'names no criterion'),
        ('nope', 'not valid JSON'),
        (json.dumps({'unobservable': [{'requirement_id': 'FR-9', 'reason': 'R'}]}),
         'does not have: FR-9'),
        (json.dumps({'unobservable': [{'requirement_id': 'FR-2', 'reason': ''}]}), 'no reason'),
    ]:
        file.write_text(content)
        with pytest.raises(RuntimeError, match=error):
            read_unobservable(ids, tmp_path)
