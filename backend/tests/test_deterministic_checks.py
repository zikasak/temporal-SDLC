"""Host checks that skip a review agent's run or decide it where they are certain
(plans/deterministic-checks.md)."""
# Assertions read optional fields directly: a None there fails the test anyway.
# pyright: reportOptionalSubscript=false, reportOptionalIterable=false
import ast
import itertools
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import config, docker_agents
from app.github.checkout import CHECKOUT_DIR
from app.implementation.activities import SEAM_DIR
from app.languages import get_language
from app.languages.python.components import COMPONENT_CONTRACT, STUB_PLACEHOLDER
from app.languages.static_checks import parse_gitleaks, parse_ruff
from app.review import activities as review
from app.review.checks import changed_static_findings

PYTHON = get_language('python')
MAKE_STUB = Path(review.__file__).parents[1] / 'languages/python/component_contract/make_stub.py'


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """The review agent: records the input it was given and reports `agent.findings`."""
    monkeypatch.setattr(review, 'GENERATED_CODE_DIR', tmp_path)
    rounds = itertools.count(1)
    monkeypatch.setattr(review, '_round_id', lambda: f'round-{next(rounds)}')

    class Agent:
        inputs: list[dict] = []
        findings: list[dict] = []

        async def run(self, role, session_id, workspace, output, *args, **kwargs):
            self.inputs.append(json.loads((workspace / 'input.json').read_text()))
            output.mkdir()
            (output / 'findings.json').write_text(json.dumps({'findings': self.findings}))

    fake = Agent()
    monkeypatch.setattr(docker_agents, 'run_agent', fake.run)
    return fake


def _write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)


# -- 1: files settled in an earlier round -----------------------------------------------

async def test_a_later_round_reviews_only_what_changed_or_was_not_judged_clean(
        tmp_path, agent, monkeypatch):
    monkeypatch.setattr(config, 'REVIEW_SKIP_SETTLED', True)
    service = tmp_path / 'session/implementation/service'
    _write(service, {'app/a.py': 'A = 1\n', 'app/b.py': 'B = 1\n'})
    shutil.copytree(PYTHON.test_suite.template, tmp_path / 'session/tests/suite')
    finding = {'summary': 'B is wrong.', 'target': 'implementation', 'reason': 'service',
               'path': 'service/app/b.py'}
    previous = {'previous_findings': [{'number': 1, **finding}]}

    async def round_(context=None, findings=()):
        agent.findings = list(findings)
        result = await review.run_review('session', {'language': 'python', **(context or {})})
        return result, agent.inputs[-1] if agent.inputs else None

    await round_(findings=[finding])
    # The finding's file is corrected; a.py, judged clean and unchanged, is not reviewed.
    (service / 'app/b.py').write_text('B = 2\n')
    _, inputs = await round_(previous)
    assert inputs['changes']['service'] == ['service/app/b.py']
    assert inputs['settled_changes']['service'] == ['service/app/a.py']
    # Nothing left to review: no agent runs, and the round is clean.
    count = len(agent.inputs)
    result, _ = await round_(previous)
    assert len(agent.inputs) == count and result['findings'] == []
    # New instructions are judged against every file again.
    _, inputs = await round_({**previous, 'instructions': 'Keep B.'})
    assert inputs['changes']['service'] == ['service/app/a.py', 'service/app/b.py']
    # A finding without a path may concern any file the run covered.
    await round_(previous, findings=[{**finding, 'path': ''}])
    _, inputs = await round_(previous)
    assert inputs['changes']['service'] == ['service/app/a.py', 'service/app/b.py']
    # Off, every round reviews every file.
    monkeypatch.setattr(config, 'REVIEW_SKIP_SETTLED', False)
    await round_(previous)
    _, inputs = await round_(previous)
    assert 'settled_changes' not in (inputs or {}) and len(inputs['changes']['service']) == 2


async def test_a_file_with_a_finding_is_reviewed_again_though_unchanged(
        tmp_path, agent, monkeypatch):
    monkeypatch.setattr(config, 'REVIEW_SKIP_SETTLED', True)
    _write(tmp_path / 'session/implementation/service', {'app/a.py': 'A = 1\n',
                                                         'app/b.py': 'B = 1\n'})
    shutil.copytree(PYTHON.test_suite.template, tmp_path / 'session/tests/suite')
    finding = {'summary': 'A is wrong.', 'target': 'implementation', 'reason': 'service',
               'path': 'service/app/a.py'}
    agent.findings = [finding]
    await review.run_review('session', {'language': 'python'})
    # A correction elsewhere may have settled it: the reviewer, not the host, decides.
    agent.findings = []
    await review.run_review('session', {'language': 'python',
                                        'previous_findings': [{'number': 1, **finding}]})
    assert agent.inputs[-1]['changes']['service'] == ['service/app/a.py']
    assert agent.inputs[-1]['settled_changes']['service'] == ['service/app/b.py']


# -- 3: requirement tags of the regression tests ----------------------------------------

def _regression(tmp_path: Path, test: str) -> dict:
    suite = tmp_path / 'session/tests/suite'
    shutil.copytree(PYTHON.test_suite.template, suite)
    _write(suite, {'tests/test_rooms.py': test})
    return {'language': 'python', 'review_scope': 'regression',
            'brief': {'requirements': [{'id': 'FR-1'}, {'id': 'FR-2'}]}}


async def test_untagged_criteria_and_unknown_tags_are_findings_without_an_agent(
        tmp_path, agent, monkeypatch):
    monkeypatch.setattr(config, 'REVIEW_REQUIREMENT_TAGS', True)
    context = _regression(tmp_path, 'def test_open():\n    # requirement: FR-1, FR-9\n'
                                    '    assert False\n')
    result = await review.run_review('session', context)
    assert agent.inputs == []
    assert result['checked_by'] == 'requirement-tags'
    summaries = [item['summary'] for item in result['findings']]
    assert len(summaries) == 2
    assert 'criterion FR-2' in summaries[0] and result['findings'][0]['path'] == 'tests/'
    assert 'tags requirement FR-9' in summaries[1]
    assert {item['target'] for item in result['findings']} == {'tests'}
    # Reported again unchanged, they repeat the findings a correction did not settle.
    again = await review.run_review('session', {**context, 'previous_findings': [
        {'number': 4, 'summary': summaries[0], 'target': 'tests', 'path': 'tests/'}]})
    assert again['findings'][0]['repeats'] == 4


async def test_complete_tags_run_the_reviewer_with_each_criterions_tests(
        tmp_path, agent, monkeypatch):
    monkeypatch.setattr(config, 'REVIEW_REQUIREMENT_TAGS', True)
    context = _regression(tmp_path, 'def test_open():\n    """requirement: FR-1"""\n\n\n'
                                    'def test_close():\n    # Requirements: fr-2\n')
    await review.run_review('session', context)
    assert agent.inputs[0]['criteria_tests'] == {'FR-1': ['tests/tests/test_rooms.py:2'],
                                                 'FR-2': ['tests/tests/test_rooms.py:6']}


# -- 2: the seam a change adds -----------------------------------------------------------

ROOMS = 'class Rooms:\n    def open(self) -> None:\n        self.state = "open"\n'
# Code that already names what the seam adds, which the added symbol may change.
MAIN = 'getattr(Rooms(), "close_quietly", print)("x")\n'
SEAM = '\n    def close_quietly(self, reason: str) -> None:\n        """Closes."""\n        ...\n'


@pytest.mark.parametrize('files, decided', [
    # Minimal and called nowhere: certainly right.
    ({'app/rooms.py': ROOMS + SEAM}, []),
    # An existing line changed.
    ({'app/rooms.py': ROOMS.replace('"open"', '"opened"') + SEAM}, ['changes or removes']),
    # A file outside the seam's modules.
    ({'app/rooms.py': ROOMS + SEAM, 'app/other.py': 'X = 1\n'}, ['not the source']),
    # Behaviour of its own, or wired in where it changes what the code does: the agent.
    ({'app/rooms.py': ROOMS + SEAM.replace('...', 'self.state = reason')}, None),
    ({'app/rooms.py': ROOMS + SEAM, 'app/main.py': MAIN}, None),
])
def test_the_host_decides_a_seam_only_where_its_diff_is_certain(tmp_path, monkeypatch, files,
                                                                 decided):
    monkeypatch.setattr(config, 'REVIEW_SEAM_CHECKS', True)
    session = tmp_path / 'session'
    main = files.get('app/main.py', 'Rooms().open()\n')
    _write(session / CHECKOUT_DIR, {'app/rooms.py': ROOMS, 'app/main.py': main})
    _write(session / SEAM_DIR / 'service', {'app/main.py': main, **files})
    _write(session / 'routing/components', {
        'runtime.json': json.dumps({'source_root': '.', 'requirements': []}),
        'app/rooms.pyi': 'class Rooms:\n    def close_quietly(self, reason: str) -> None: ...\n'})
    seam = {'module': 'app.rooms', 'symbols': ['Rooms.close_quietly'],
            'added': ['Rooms.close_quietly']}
    context = {'review_scope': 'seam', 'seam': {'app.rooms': ['Rooms.close_quietly']},
               'component_routes': {'FR-1': {'route': 'component', 'seam': seam}}}
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    prepared = review.plan_review(PYTHON, session, context, workspace)
    if decided is None:
        assert prepared.decided is None and prepared.changes['service']
    else:
        assert prepared.decided is not None
        assert [item['target'] for item in prepared.decided] == ['implementation'] * len(decided)
        assert all(text in item['summary'] for text, item in zip(decided, prepared.decided))


# -- 4: static tools ---------------------------------------------------------------------

def test_static_results_reach_the_reviewer_only_on_added_lines_and_never_the_secret():
    diff = ('--- a/service/app.py\n+++ b/service/app.py\n@@ -1,2 +1,3 @@\n'
            ' import os\n+TOKEN = "ghp_secret"\n x = 1\n')
    ruff = json.dumps([
        {'code': 'S105', 'message': 'Hardcoded password', 'filename':
         '/workspace/review/service/app.py', 'location': {'row': 2, 'column': 1}},
        {'code': 'F401', 'message': 'unused import', 'filename':
         '/workspace/review/service/app.py', 'location': {'row': 1, 'column': 1}}])
    gitleaks = json.dumps([{'RuleID': 'github-pat', 'Description': 'GitHub token',
                            'File': 'service/app.py', 'StartLine': 2,
                            'Secret': 'ghp_secret', 'Match': 'TOKEN = "ghp_secret"'}])
    results = [{**item, 'tool': 'ruff'} for item in parse_ruff(ruff)] + [
        {**item, 'tool': 'gitleaks'} for item in parse_gitleaks(gitleaks)]
    kept = changed_static_findings(results, {'service/app.py': diff})
    assert [(item['tool'], item['rule'], item['line']) for item in kept] == [
        ('ruff', 'S105', 2), ('gitleaks', 'github-pat', 2)]
    assert 'ghp_secret' not in json.dumps(kept)


# -- 5: stubs generated from the code ----------------------------------------------------

SOURCE = '''from __future__ import annotations
from dataclasses import dataclass
from typing import Final, Literal
from temporalio import workflow
from app.store import Store

QUEUE: Final = "rooms"
State = Literal["open", "closed"]


@dataclass
class Room:
    name: str
    size: int = 2


class Rooms:
    def __init__(self, store: Store, limit: int = len("x")) -> None:
        self.store = store

    @workflow.signal
    async def close(self, room: Room, reason: str | None = None) -> 'State':
        return "closed"

    @property
    def state(self) -> State:
        return "open"


def count(rooms: list[Room]) -> int:
    return len(rooms)
'''


def _make_stub(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(MAKE_STUB), *arguments], cwd=root,
                          capture_output=True, text=True)


def _described(text: str) -> str:
    return text.replace(STUB_PLACEHOLDER, 'Does what the brief says.')


def test_a_generated_stub_has_the_codes_signatures_and_is_a_valid_contract(tmp_path):
    _write(tmp_path / 'repository', {'app/rooms.py': SOURCE,
                                     'app/store.py': 'class Store: ...\n'})
    result = _make_stub(tmp_path, 'app.rooms', 'Rooms.close', 'Rooms.state', 'count', 'QUEUE')
    assert result.returncode == 0, result.stderr
    text = (tmp_path / 'components/app/rooms.pyi').read_text()
    stub = {node.name: node for node in ast.walk(ast.parse(text))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert ast.unparse(stub['close'].args) == 'self, room: Room, reason: str | None=None'
    # A type another module of the repository defines is stubbed there and imported.
    assert ast.unparse(stub['__init__'].args) == 'self, store: Store, limit: int=...'
    assert 'from app.store import Store' in text
    assert 'Store' in COMPONENT_CONTRACT.read_stub(
        'app.store', (tmp_path / 'components/app/store.pyi').read_text())
    assert [ast.unparse(item) for item in stub['state'].decorator_list] == ['property']
    # The dataclass a signature names comes with the fields its __init__ is built from.
    assert 'class Room' in text and 'name: str' in text and "QUEUE: Final = 'rooms'" in text
    # Named symbols carry the placeholder until the agent states their behaviour.
    with pytest.raises(ValueError, match='placeholder'):
        COMPONENT_CONTRACT.read_stub('app.rooms', text)
    assert {'Rooms.close', 'Rooms.state', 'Rooms.__init__', 'count', 'Room', 'State',
            'QUEUE'} <= COMPONENT_CONTRACT.read_stub('app.rooms', _described(text))


def test_types_of_other_modules_are_stubbed_where_they_are_defined(tmp_path):
    _write(tmp_path / 'repository', {
        'app/__init__.py': '',
        'app/publish.py': ('import app.layout as layout\n'
                           'from app.models import Contract\n\n\n'
                           'async def publish(workspace: layout.Workspace, '
                           'contract: Contract) -> None:\n    pass\n'),
        # A re-export, and a type whose module names a type of the first one back.
        'app/models.py': 'from app.contract import Contract\n',
        'app/contract.py': ('from typing import TypedDict\n'
                            'from app.layout import Workspace\n\n\n'
                            'class Contract(TypedDict):\n    language: str\n'
                            '    workspace: Workspace\n'),
        'app/layout.py': ('from dataclasses import dataclass\nfrom pathlib import Path\n\n\n'
                          '@dataclass(frozen=True)\nclass Workspace:\n    root: Path\n\n'
                          '    @property\n    def checkout(self) -> Path:\n'
                          '        return self.root / "repository"\n\n'
                          '    def _hidden(self) -> int:\n        return 1\n'),
    })
    result = _make_stub(tmp_path, 'app.publish', 'publish')
    assert result.returncode == 0, result.stderr
    assert 'Any' not in (tmp_path / 'components/app/publish.pyi').read_text()
    layout = COMPONENT_CONTRACT.read_stub(
        'app.layout', (tmp_path / 'components/app/layout.pyi').read_text())
    # A test builds the type from its fields and reads its public properties.
    assert {'Workspace', 'Workspace.root', 'Workspace.checkout'} <= layout
    assert 'Workspace._hidden' not in layout
    assert 'from app.contract import Contract' in (
        tmp_path / 'components/app/models.pyi').read_text()
    assert {'Contract', 'Contract.workspace'} <= COMPONENT_CONTRACT.read_stub(
        'app.contract', (tmp_path / 'components/app/contract.pyi').read_text())


def test_the_host_rejects_a_new_stub_statement_that_erases_a_type_of_the_code(tmp_path):
    from app.routing.components import read_components
    runtime = '{"source_root": ".", "requirements": ["requirements.txt"]}'
    code = ('from app.layout import Workspace\n\n\n'
            'async def publish(workspace: Workspace, kind: str) -> dict:\n    pass\n\n\n'
            'async def close(workspace: Workspace) -> None:\n    pass\n')
    published = ('from typing import Any\n\n\n'
                 'async def close(workspace: Any) -> None:\n'
                 '    """Close it."""\n    ...\n')
    _write(tmp_path, {'repository/requirements.txt': '',
                      'repository/app/activities.py': code,
                      'repository/contracts/components/runtime.json': runtime,
                      'repository/contracts/components/app/activities.pyi': published,
                      'components/runtime.json': runtime})
    stub = tmp_path / 'components/app/activities.pyi'
    targets = itertools.count()

    def read() -> None:
        read_components(COMPONENT_CONTRACT, tmp_path / 'components', tmp_path / 'repository',
                        tmp_path / 'repository/contracts/components',
                        tmp_path / f'target-{next(targets)}')

    added = ('\n\nasync def publish(workspace: {}, kind: str) -> dict:\n'
             '    """Push it."""\n    ...\n')
    stub.parent.mkdir(parents=True)
    stub.write_text(published + added.format('Any'))
    with pytest.raises(RuntimeError, match=r'publish \(argument workspace Workspace\)'):
        read()
    # The published statement keeps its Any: only what the stub adds is checked.
    stub.write_text('from app.layout import Workspace\n' + published + added.format('Workspace'))
    read()


def test_a_published_stub_is_only_extended(tmp_path):
    _write(tmp_path / 'repository', {'app/rooms.py': SOURCE,
                                     'app/store.py': 'class Store: ...\n'})
    published = 'class Rooms:\n    def __init__(self, store, limit=...) -> None: ...\n'
    _write(tmp_path, {'components/app/rooms.pyi': published,
                      'repository/contracts/components/app/rooms.pyi': published})
    # A published class may not gain a member: its statement would change.
    result = _make_stub(tmp_path, 'app.rooms', 'Rooms.close')
    assert result.returncode == 1 and 'published' in result.stderr
    assert (tmp_path / 'components/app/rooms.pyi').read_text() == published
    assert _make_stub(tmp_path, 'app.rooms', 'count').returncode == 0
    text = _described((tmp_path / 'components/app/rooms.pyi').read_text())
    kept = COMPONENT_CONTRACT.statements(text)
    assert all(item in kept for item in COMPONENT_CONTRACT.statements(published))
    assert 'count' in COMPONENT_CONTRACT.read_stub('app.rooms', text)
    # A class it wrote itself does gain members, and an imported type's stub is used.
    (tmp_path / 'components/app/store.pyi').write_text('class Store: ...\n')
    (tmp_path / 'components/app/rooms.pyi').unlink()
    (tmp_path / 'repository/contracts').rename(tmp_path / 'unpublished')
    assert _make_stub(tmp_path, 'app.rooms', 'Rooms.close').returncode == 0
    assert _make_stub(tmp_path, 'app.rooms', 'Rooms.state').returncode == 0
    text = (tmp_path / 'components/app/rooms.pyi').read_text()
    assert 'from app.store import Store' in text and 'store: Store' in text
    assert {'Rooms.close', 'Rooms.state'} <= COMPONENT_CONTRACT.read_stub(
        'app.rooms', _described(text))


def test_a_symbol_the_code_lacks_is_left_to_the_agent(tmp_path):
    _write(tmp_path / 'repository', {'app/rooms.py': SOURCE})
    result = _make_stub(tmp_path, 'app.rooms', 'Rooms.reopen')
    assert result.returncode == 1 and 'hand-write' in result.stderr


def test_a_changed_published_statement_is_named_to_the_agent_and_by_the_host(tmp_path):
    from app.routing.components import read_components
    runtime = '{"source_root": ".", "requirements": ["requirements.txt"]}'
    published = ('from typing import Any\n\n\n'
                 'async def publish(session_id: str) -> Any:\n'
                 '    """Push the branch."""\n    ...\n')
    _write(tmp_path, {'repository/requirements.txt': '',
                      'repository/contracts/components/runtime.json': runtime,
                      'repository/contracts/components/app/activities.pyi': published,
                      'components/runtime.json': runtime})
    stub = tmp_path / 'components/app/activities.pyi'
    stub.parent.mkdir(parents=True)

    def read() -> None:
        read_components(COMPONENT_CONTRACT, tmp_path / 'components', tmp_path / 'repository',
                        tmp_path / 'repository/contracts/components',
                        tmp_path / f'target-{len(list(tmp_path.glob("target-*")))}')

    # The import gains a name and the published docstring a sentence: both are changes.
    stub.write_text(published.replace('import Any', 'import Any, Final')
                    .replace('branch.', 'branch, with a written message.')
                    + '\n\nSUBJECT: Final = "Change"\n')
    result = _make_stub(tmp_path, '--check')
    assert result.returncode == 1
    assert '`from typing import Any`' in result.stderr
    assert '`async def publish(session_id: str) -> Any:`' in result.stderr
    with pytest.raises(RuntimeError, match=r'\(from typing import Any\)'):
        read()
    # The same names added as statements of their own keep the published stub.
    stub.write_text(published + '\nfrom typing import Final\n\nSUBJECT: Final = "Change"\n')
    assert _make_stub(tmp_path, '--check').returncode == 0
    read()
