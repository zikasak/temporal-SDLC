"""Seam modules named in path form are matched to the module the component contract
stubs; a module with no stub still fails the routing."""
import json
from pathlib import Path

import pytest

from app.routing.agent import match_module, validate_completion, validate_final
from app.routing.components import check_seams

MODULES = {'app.docker_agents': {'run_agent', 'docker'}, 'app.routing.agent': {'ROUTES'}}


def _route(module: str, **seam) -> dict:
    return {'route': 'component', 'reason': 'r',
            'seam': {'module': module, 'symbols': ['run_agent'], **seam}}


@pytest.mark.parametrize('module', ['app/docker_agents', 'app/docker_agents.py',
                                    'app.docker_agents'])
def test_path_form_seam_module_is_recorded_dotted(module: str) -> None:
    routes = {'FR-1': _route(module)}
    check_seams(routes, MODULES)
    assert routes['FR-1']['seam']['module'] == 'app.docker_agents'


def test_path_form_added_elsewhere_module_is_recorded_dotted() -> None:
    routes = {'FR-1': _route('app.routing.agent', symbols=['ROUTES'],
                             added_elsewhere={'app/docker_agents.py': ['docker']})}
    check_seams(routes, MODULES)
    assert routes['FR-1']['seam']['added_elsewhere'] == {'app.docker_agents': ['docker']}


@pytest.mark.parametrize('module', ['app/missing', 'app.missing'])
def test_module_without_stub_still_fails(module: str) -> None:
    with pytest.raises(RuntimeError) as error:
        check_seams({'FR-1': _route(module)}, MODULES)
    assert str(error.value) == ('The seam of requirement FR-1 names a module the component '
                                f'contract has no stub of: {module}')


def test_ambiguous_path_matches_nothing() -> None:
    assert match_module('a/b', ['a.b', 'a/b']) == 'a/b'
    assert match_module('a/b.py', ['a.b', 'a/b']) is None


@pytest.mark.parametrize('name', ['app/languages/java', 'app/languages/java/__init__.py',
                                  'app/languages/java.py', 'app.languages.java'])
def test_module_named_like_a_suffix_keeps_its_last_segment(name: str) -> None:
    modules = ['app.languages', 'app.languages.java', 'app.languages.java.suite']
    assert match_module(name, modules) == 'app.languages.java'
    assert match_module('app/languages', modules) == 'app.languages'


def _workspace(tmp_path: Path, seam: dict) -> Path:
    (tmp_path / 'components' / 'app').mkdir(parents=True)
    (tmp_path / 'components' / 'app' / 'docker_agents.pyi').write_text('def run_agent(): ...\n')
    (tmp_path / 'routes.json').write_text(json.dumps({'routes': [
        {'requirement_id': 'FR-1', 'route': 'component', 'reason': 'r', 'seam': seam}]}))
    return tmp_path


def test_agent_accepts_path_form_of_a_stub(tmp_path: Path) -> None:
    validate_completion(_workspace(tmp_path, {'module': 'app/docker_agents.py',
                                              'symbols': ['run_agent']}))


@pytest.mark.parametrize('seam', [
    {'module': 'app/missing', 'symbols': ['x']},
    {'module': 'app.docker_agents', 'symbols': ['run_agent'],
     'added_elsewhere': {'app/missing': ['x']}},
])
def test_agent_is_told_of_unmatched_modules(tmp_path: Path, seam: dict) -> None:
    with pytest.raises(RuntimeError) as error:
        validate_completion(_workspace(tmp_path, seam))
    assert 'app/missing' in str(error.value)
    assert 'app.docker_agents' in str(error.value)


def test_final_check_leaves_unmatched_modules_to_check_seams(tmp_path: Path) -> None:
    # The host's check_seams then fails with its own message.
    validate_final(_workspace(tmp_path, {'module': 'app/missing', 'symbols': ['x']}))
