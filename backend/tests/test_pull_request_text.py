"""What the pull request of a pushed delivery says about it."""
from app.delivery.pull_request import BODY_CHARS, LISTED_CRITERIA, describe

PUBLICATION = {'branch': 'generated/abc', 'verified': True, 'findings': [], 'findings_count': 0}
SEAM = {'module': 'app.delivery.workflows', 'symbols': ['DeliveryWorkflow.close']}


def test_a_routed_change_lists_which_suite_proves_each_criterion():
    change = {'kind': 'bugfix', 'proofs': {
        'component': [{'id': 'FR-5', 'title': 'Closed flows stay closed', 'seam': SEAM}],
        'contract': [{'id': 'FR-2', 'title': 'Rooms do not double-book'}],
    }}
    title, body = describe('Fix the service', change, PUBLICATION,
                           {'title': 'Fix closing', 'summary': 'What changed.'})
    assert title == 'Fix closing'
    lines = body.splitlines()
    assert lines[0] == 'What changed.'
    proofs = lines[lines.index('## How the criteria are proved'):]
    # The contract tests come first, whichever order the routing had.
    assert proofs.index('- FR-2: Rooms do not double-book') < proofs.index(
        '- FR-5: Closed flows stay closed (at `app.delivery.workflows`: '
        '`DeliveryWorkflow.close`)')
    assert any(line.startswith('Component tests (`component-tests/`)') for line in proofs)


def test_a_change_without_routes_says_nothing_of_suites():
    _, body = describe('Fix the service', {'kind': 'bugfix'}, PUBLICATION)
    assert 'How the criteria are proved' not in body


def test_the_proofs_and_findings_always_fit_and_long_lists_are_counted():
    criteria = [{'id': f'FR-{index}', 'title': 'T' * 500} for index in range(LISTED_CRITERIA + 3)]
    publication = {**PUBLICATION, 'verified': False, 'findings_count': 1, 'findings': [
        {'summary': 'The docstring names a private helper.', 'target': 'both', 'reason': 'R',
         'path': 'contracts/components/app/rooms.pyi'}]}
    _, body = describe('Fix the service', {'kind': 'bugfix', 'proofs': {'contract': criteria}},
                       publication, {'title': 'Fix', 'summary': 'S' * 70000})
    assert len(body) <= BODY_CHARS
    assert '- and 3 more' in body
    # The summary is cut, never the proofs or the findings after it.
    assert 'Open review findings: 1' in body
    assert 'The docstring names a private helper.' in body


def test_the_proofs_name_the_symbols_of_a_seam_the_change_added():
    seam = {'module': 'app.delivery.workflows',
            'symbols': ['DeliveryWorkflow.pull_request_closed', 'DeliveryWorkflow.get_state'],
            'added': ['DeliveryWorkflow.pull_request_closed']}
    _, body = describe('Fix the service', {'kind': 'bugfix', 'proofs': {
        'component': [{'id': 'FR-1', 'title': 'Merged flows complete', 'seam': seam}]}},
        PUBLICATION)
    assert ('- FR-1: Merged flows complete (at `app.delivery.workflows`: '
            '`DeliveryWorkflow.pull_request_closed`, `DeliveryWorkflow.get_state`; this change '
            'adds `DeliveryWorkflow.pull_request_closed`)') in body.splitlines()


def test_the_proofs_name_the_part_of_a_seam_outside_the_service():
    part_seam = {'part': 'frontend', 'module': 'src/Flows', 'symbols': ['sessionAttention'],
                 'added': ['sessionAttention']}
    _, body = describe('Fix the service', {'kind': 'bugfix', 'proofs': {'component': [
        {'id': 'FR-5', 'title': 'Closed flows stay closed', 'seam': SEAM},
        {'id': 'FR-115', 'title': 'Closed flows are not waiting', 'seam': part_seam},
    ]}}, PUBLICATION)
    lines = body.splitlines()
    assert ('- FR-115: Closed flows are not waiting (at `frontend: src/Flows`: '
            '`sessionAttention`; this change adds `sessionAttention`)') in lines
    # The service's seam is named as before.
    assert ('- FR-5: Closed flows stay closed (at `app.delivery.workflows`: '
            '`DeliveryWorkflow.close`)') in lines
    # The heading says where each part's tests and stubs are, the service's first.
    assert ("Component tests, in-process at seams of the code: the service's in "
            "`component-tests/`, with stubs in `contracts/components/`; part `frontend`'s in "
            "`frontend/component-tests/`, with stubs in `contracts/parts/frontend/components/`:"
            ) in lines


def test_the_proofs_of_another_part_alone_name_only_its_tests():
    seam = {'part': 'frontend', 'module': 'src/github', 'symbols': ['createGitHubApp']}
    _, body = describe('Fix the service', {'kind': 'bugfix', 'proofs': {'component': [
        {'id': 'TR-6', 'title': 'The app hears of closed pull requests', 'seam': seam}]}},
        PUBLICATION)
    assert ("Component tests, in-process at seams of the code: part `frontend`'s in "
            "`frontend/component-tests/`, with stubs in `contracts/parts/frontend/components/`:"
            ) in body.splitlines()
