"""The reviewer's findings as the host accepts them."""
import json

import pytest

from app.delivery.models import named_tests
from app.review.activities import read_findings

PREVIOUS = [{'number': 1, 'summary': 'GET /rooms  does not sort.', 'target': 'implementation',
             'path': 'service/Rooms.java'},
            {'number': 2, 'summary': 'POST /rooms accepts no name.', 'target': 'tests'}]


def findings(tmp_path, items, previous=PREVIOUS):
    (tmp_path / 'findings.json').write_text(json.dumps({'findings': items}))
    return read_findings(tmp_path, {'service/Rooms.java'}, previous)


def test_findings_are_marked_with_the_earlier_finding_they_repeat(tmp_path):
    base = {'target': 'implementation', 'reason': 'service', 'path': 'service/Rooms.java'}
    result = findings(tmp_path, [
        # Named by the reviewer.
        {**base, 'summary': 'Rooms are still unsorted.', 'repeats': 2},
        # Reported in the same words, though the reviewer did not say so.
        {**base, 'summary': 'GET /rooms does not   sort.'},
        # A number no earlier finding has, and a new finding.
        {**base, 'summary': 'GET /rooms pages wrongly.', 'repeats': 7},
    ])
    assert [item.get('repeats') for item in result] == [2, 1, None]
    # Without earlier findings nothing repeats.
    assert all('repeats' not in item for item in findings(tmp_path, [
        {**base, 'summary': 'GET /rooms does not sort.', 'repeats': 1}], previous=None))


def test_a_change_with_component_tests_has_its_tests_and_stubs_reviewed(tmp_path):
    import shutil

    from app.languages import get_language
    from app.review import activities as review
    from app.review import agent

    language = get_language('python')
    session = tmp_path / 'session'
    files = {
        # The repository as the delivery found it.
        'checkout/app/rooms.py': 'def close(): pass\n',
        'checkout/contracts/components/app/rooms.pyi': 'def close() -> None: ...\n',
        'repository-tests/components/tests/test_rooms.py': 'def test_open(): pass\n',
        # What the delivery made of it.
        'implementation/service/app/rooms.py': 'def close(): return None\n',
        'component-tests/suite/tests/test_rooms.py': 'def test_open(): pass\n'
                                                     'def test_closed(): pass\n',
        'routing/components/app/rooms.pyi': 'def close() -> None:\n    """Closes."""\n',
        'routing/components/runtime.json': '{}',
    }
    for path, text in files.items():
        (session / path).parent.mkdir(parents=True, exist_ok=True)
        (session / path).write_text(text)
    context = {'component_routes': {'FR-2': {'route': 'component'}}, 'contract_tests': False}
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    changes, _ = review.prepare_review(language, session, context, workspace)
    # No contract tests of the change's own: only the service, its component tests and
    # the stubs that differ from the repository's.
    assert changes == {'service': ['service/app/rooms.py'],
                       'component-tests': ['component-tests/tests/test_rooms.py'],
                       'contracts/components': ['contracts/components/app/rooms.pyi',
                                                'contracts/components/runtime.json']}
    inputs = json.loads((workspace / 'input.json').read_text())
    assert inputs['contract_tests'] is False and inputs['component_routes']
    assert (workspace / 'review/contracts/components/app/rooms.pyi').is_file()
    assert not (workspace / 'review/tests').exists()
    diff = (workspace / 'review/changes.diff').read_text()
    assert '+def test_closed(): pass' in diff and '+    """Closes."""' in diff
    # With contract tests of its own, they are reviewed too.
    shutil.copytree(language.test_suite.template, session / 'tests/suite')
    other = tmp_path / 'other'
    other.mkdir()
    changes, _ = review.prepare_review(language, session, {**context, 'contract_tests': True},
                                       other)
    assert set(changes) == {'service', 'tests', 'component-tests', 'contracts/components'}
    # The stubs' docstrings are the reviewer's to check, and its findings about them the
    # user's to decide.
    assert 'contracts/components/' in agent.COMPONENT_PROMPT
    findings = review.read_findings(_written(tmp_path, [
        {'summary': 'Leaks a private helper.', 'target': 'both',
         'path': 'contracts/components/app/rooms.pyi'}]),
        {'contracts/components/app/rooms.pyi'})
    assert findings[0].get('path') == 'contracts/components/app/rooms.pyi'


def test_a_change_with_component_tests_of_its_web_client_has_that_parts_reviewed(tmp_path):
    from app.languages import get_language
    from app.review import activities as review

    language = get_language('python')
    session = tmp_path / 'session'
    files = {
        'checkout/frontend/src/github.ts': 'export function createGitHubApp() {}\n',
        'checkout/contracts/parts/frontend/components/src/github.d.ts': 'old stub\n',
        # Recorded before any author ran: the web client's tests, and the service's own,
        # which this change has no criterion of.
        'repository-tests/components/parts/frontend/tests/a.test.ts': 'test("a")\n',
        'repository-tests/components/parts/frontend/package.json': '{}',
        'repository-tests/components/tests/test_rooms.py': 'def test_open(): pass\n',
        'implementation/service/frontend/src/github.ts': 'export function createGitHubApp() {1}\n',
        'component-tests/suite/parts/frontend/tests/a.test.ts': 'test("a")\ntest("b")\n',
        'component-tests/suite/parts/frontend/package.json': '{}',
        'routing/routes.json': json.dumps({'routes': {}, 'parts': [
            {'name': 'frontend', 'root': 'frontend', 'language': 'typescript'}]}),
        'routing/parts/frontend/components/src/github.d.ts': 'old stub\nnew stub\n',
        'routing/parts/frontend/components/runtime.json': '{"project": "frontend"}',
    }
    for path, text in files.items():
        (session / path).parent.mkdir(parents=True, exist_ok=True)
        (session / path).write_text(text)
    context = {'component_routes': {'TR-6': {'route': 'component'}}, 'contract_tests': False}
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    changes, _ = review.prepare_review(language, session, context, workspace)
    # The service's component tests, which the suite has no project of, are not reported
    # as removed.
    assert changes == {
        'service': ['service/frontend/src/github.ts'],
        'component-tests': ['component-tests/parts/frontend/tests/a.test.ts'],
        'contracts/parts/frontend/components': [
            'contracts/parts/frontend/components/runtime.json',
            'contracts/parts/frontend/components/src/github.d.ts']}
    assert (workspace / 'review/contracts/parts/frontend/components/src/github.d.ts').is_file()


def _written(tmp_path, items):
    output = tmp_path / 'findings-output'
    output.mkdir(exist_ok=True)
    (output / 'findings.json').write_text(json.dumps({'findings': items}))
    return output


def test_findings_about_the_stubs_go_to_no_author_and_tests_findings_to_their_suite():
    from app.review.models import (
        ReviewFinding, correctable, for_suite, for_worker, review_request,
    )
    stub: ReviewFinding = {'summary': 'S', 'target': 'both', 'reason': 'R', 'path': 'contracts/components/a.pyi'}
    component: ReviewFinding = {'summary': 'C', 'target': 'tests', 'reason': 'R',
                 'path': 'component-tests/tests/test_a.py'}
    contract: ReviewFinding = {'summary': 'H', 'target': 'tests', 'reason': 'R', 'path': 'tests/test_a.py'}
    anywhere: ReviewFinding = {'summary': 'A', 'target': 'tests', 'reason': 'R'}
    findings = [stub, component, contract, anywhere]
    assert not correctable(stub) and all(map(correctable, findings[1:]))
    assert for_worker(findings, 'implementation') == []
    assert for_worker(findings, 'tests') == [component, contract, anywhere]
    tests = review_request(findings, 'tests', 1)['review']['findings']
    assert [item['summary'] for item in for_suite(tests, 'component')] == ['C', 'A']
    assert [item['summary'] for item in for_suite(tests, 'contract')] == ['H', 'A']


def _session(tmp_path, files):
    session = tmp_path / 'session'
    for path, text in files.items():
        (session / path).parent.mkdir(parents=True, exist_ok=True)
        (session / path).write_text(text)
    return session


# A change whose criterion is observed at a seam it added first, before its fix.
SEAM_FILES = {
    'checkout/app/rooms.py': 'def close(): pass\n',
    'checkout/contracts/components/app/rooms.pyi': 'def close() -> None: ...\n',
    'repository-tests/components/tests/test_rooms.py': 'def test_open(): pass\n',
    'seam/service/app/rooms.py': 'def close(): pass\ndef merged(): return None\n',
    'routing/components/app/rooms.pyi': 'def close() -> None: ...\ndef merged() -> None: ...\n',
    'routing/components/runtime.json': '{}',
}


def test_a_seam_is_reviewed_alone_with_its_stubs_beside_it(tmp_path):
    from app.languages import get_language
    from app.review import activities as review
    from app.review import agent

    language = get_language('python')
    session = _session(tmp_path, SEAM_FILES)
    context = {'review_scope': 'seam', 'seam': {'app.rooms': ['merged']},
               'component_routes': {'FR-2': {'route': 'component'}}}
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    changes, _ = review.prepare_review(language, session, context, workspace)
    # Only the code with the seam is under review: no tests exist yet, and the stubs are
    # there to read.
    assert changes == {'service': ['service/app/rooms.py']}
    assert (workspace / 'review/contracts/components/app/rooms.pyi').is_file()
    assert '+def merged(): return None' in (workspace / 'review/changes.diff').read_text()
    inputs = json.loads((workspace / 'input.json').read_text())
    assert inputs['review_scope'] == 'seam' and inputs['seam'] == {'app.rooms': ['merged']}
    prompt = agent.prompt(inputs, 'Python')
    assert prompt.startswith('You review a seam') and 'service/ or contracts/' in prompt
    assert agent.COMPONENT_PROMPT not in prompt


def test_regression_tests_are_reviewed_before_the_fix_beside_the_code_they_ran_against(tmp_path):
    import shutil

    from app.languages import get_language
    from app.review import activities as review
    from app.review import agent

    language = get_language('python')
    session = _session(tmp_path, {
        **SEAM_FILES,
        'component-tests/suite/tests/test_rooms.py': 'def test_open(): pass\n'
                                                     'def test_merged(): assert False\n',
    })
    shutil.copytree(language.test_suite.template, session / 'tests/suite')
    baseline = {'component': [{'class_name': 'tests.test_rooms', 'name': 'test_merged',
                               'kind': 'failure', 'message': 'assert False'}]}
    context = {'review_scope': 'regression', 'seam_prepared': True, 'contract_tests': True,
               'component_routes': {'FR-2': {'route': 'component'}}, 'baseline': baseline}
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    changes, _ = review.prepare_review(language, session, context, workspace)
    # The tests are under review; the code with the seam is beside them, unchanged.
    assert set(changes) == {'tests', 'component-tests'}
    assert changes['component-tests'] == ['component-tests/tests/test_rooms.py']
    assert 'merged' in (workspace / 'review/service/app/rooms.py').read_text()
    assert 'service/' not in (workspace / 'review/changes.diff').read_text()
    inputs = json.loads((workspace / 'input.json').read_text())
    assert inputs['baseline'] == baseline and inputs['contract_tests'] is True
    prompt = agent.prompt(inputs, 'Python')
    assert prompt.startswith('You review the regression tests')
    assert agent.REGRESSION_COMPONENT_PROMPT in prompt


def test_a_stage_review_sends_every_finding_to_its_one_worker(tmp_path):
    from app.review.activities import read_findings

    output = _written(tmp_path, [
        {'summary': 'Calls the new query from close().', 'target': 'both',
         'path': 'service/app/rooms.py'}])
    finding, = read_findings(output, {'service/app/rooms.py'}, None, 'implementation')
    assert finding['target'] == 'implementation'


def test_the_delivery_review_prompt_is_as_it_was(tmp_path):
    from app.review import agent

    prompt = agent.prompt({'brief': {'requirements': []}}, 'Java')
    assert prompt.startswith('You review the changes a delivery made to a Java service')
    assert 'Do not change service/ or tests/ and do not run the tests.' in prompt
    assert agent.CHANGE_PROMPT in prompt
    assert agent.BUGFIX_PROMPT not in prompt


def test_a_bugfix_review_asks_only_for_tests_that_can_fail_without_the_fix():
    from app.review import agent

    prompt = agent.prompt({'brief': {'requirements': []}, 'regression': True}, 'Java')
    assert agent.BUGFIX_PROMPT in prompt


ARCHITECTURE = 'tests.test_architecture#test_workflow_listing_accepts_the_architecture_type'
LISTING = 'tests.test_onboarding#test_workflow_listing_accepts_the_onboarding_type'


@pytest.mark.parametrize('finding, named', [
    # The test by its name and its module, in the file the finding is about.
    ({'target': 'tests', 'path': 'tests/tests/test_architecture.py',
      'summary': 'test_workflow_listing_accepts_the_architecture_type still asserts the old '
                 'parent.'}, [ARCHITECTURE]),
    # The suite's root package names every test: it does not tell them apart.
    ({'target': 'tests', 'path': 'tests/tests/test_other.py',
      'summary': 'test_workflow_listing_accepts_the_architecture_type is fine; the other is not.'},
     []),
    # A finding for the implementation releases nothing.
    ({'target': 'implementation', 'path': 'tests/tests/test_architecture.py',
      'summary': 'test_workflow_listing_accepts_the_architecture_type fails.'}, []),
])
def test_a_finding_about_the_tests_names_the_kept_tests_it_concerns(finding, named):
    assert named_tests({'reason': 'R', **finding}, [ARCHITECTURE, LISTING]) == named
