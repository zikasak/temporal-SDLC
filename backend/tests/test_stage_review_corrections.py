"""What the authors of a change's seam and regression tests are told when they correct the
findings of a review before the fix."""
from app.implementation import agent as implementation
from app.test_generation import agent as tests

REVIEW = {'attempt': 1, 'review': {'findings': [{'summary': 'S', 'reason': 'R'}]}}
LANGUAGE = {'instructions': '', 'test_glob': '**/*.py'}


def test_the_seam_author_corrects_its_seam_and_still_adds_nothing_else():
    inputs = {'brief': {'requirements': []}, 'seam': {'app.rooms': ['merged']},
              'repair': REVIEW}
    prompt = implementation.base_prompt(inputs, '') + implementation.repair_prompt(inputs)
    assert implementation.SEAM_PROMPT.format(instructions='') in prompt
    assert prompt.endswith(implementation.SEAM_REVIEW_PROMPT)
    # The fix's correction after the delivery's review is told apart.
    fix = {'brief': {'requirements': []}, 'repair': REVIEW}
    assert implementation.repair_prompt(fix) == implementation.REVIEW_PROMPT


def test_regression_tests_corrected_before_the_fix_must_still_fail_without_it():
    before = {**REVIEW, 'review': {**REVIEW['review'], 'before_fix': True}}
    contract = tests.prompt({'brief': {'requirements': []}, 'repair': before}, LANGUAGE)
    assert tests.REGRESSION_REVIEW_PROMPT in contract and tests.REVIEW_PROMPT not in contract
    component = tests.prompt({'component': True, 'repair': before}, LANGUAGE)
    assert tests.COMPONENT_REGRESSION_REVIEW_PROMPT in component
    assert tests.COMPONENT_REVIEW_PROMPT not in component
    # After the fix, a review's findings are corrected as before.
    after = tests.prompt({'brief': {'requirements': []}, 'repair': REVIEW}, LANGUAGE)
    assert tests.REVIEW_PROMPT in after


def test_a_correction_of_the_seam_starts_from_the_seam_as_it_was_added(tmp_path):
    from app.github.checkout import CHECKOUT_DIR
    from app.implementation.activities import SEAM_DIR, prepare_inputs
    from app.languages import get_language

    language = get_language('python')
    for where, text in ((CHECKOUT_DIR, 'repository'), (f'{SEAM_DIR}/service', 'seam'),
                        ('implementation/service', 'fix')):
        (tmp_path / where / 'app').mkdir(parents=True)
        (tmp_path / where / 'app' / 'rooms.py').write_text(text)
    context = {'repository': {'full_name': 'o/r'}, 'brief': {'requirements': []},
               'seam': {'app.rooms': ['merged']}, 'repair': REVIEW}
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    prepare_inputs(language, tmp_path, context).populate(workspace)
    assert (workspace / 'service/app/rooms.py').read_text() == 'seam'
