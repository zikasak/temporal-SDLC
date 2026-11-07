"""Bounded code review payloads exchanged between the review and the authors."""
from typing import Any, Literal, NotRequired, TypedDict

ReviewTarget = Literal['implementation', 'tests', 'both']
REVIEW_TARGETS = ('implementation', 'tests', 'both')
# Each run of review rounds, including one started by the user's instructions, reviews
# the delivery at most this often; test repair rounds have a limit of their own.
MAX_REVIEW_ROUNDS = 5
# What a review before the fix looks at, when it is not the whole verified delivery: the
# seam a change added first, or its regression tests once they were seen failing without
# the fix. Each is reviewed and corrected in rounds of its own before the next stage
# starts, at most MAX_STAGE_REVIEW_ROUNDS times; the final review sees them again.
ReviewScope = Literal['seam', 'regression']
SCOPE_KEY = 'review_scope'
# The one worker a stage review's findings go to.
SCOPE_TARGETS: dict[str, ReviewTarget] = {'seam': 'implementation', 'regression': 'tests'}
MAX_STAGE_REVIEW_ROUNDS = 3
# How a change's regression tests failed without the fix, in a regression review's context.
BASELINE_KEY = 'baseline'
MAX_FINDINGS = 50
SUMMARY_CHARS = 2000
REASON_CHARS = 500
PATH_CHARS = 500
# The user's instructions about open findings have at most 10000 characters, as the
# HTTP API accepts them.
INSTRUCTIONS_CHARS = 10000
# A fix request from a pull request comment has up to GitHub's 65536 characters, and the
# review reads all of it; this bounds instructions the review receives internally.
REVIEW_INSTRUCTIONS_CHARS = 65536
# How much of each earlier finding a later review round receives.
PREVIOUS_SUMMARY_CHARS = 1000
# The open findings a pushed commit's message lists.
COMMIT_FINDINGS = 20


class ReviewFinding(TypedDict):
    """A problem the code review found in the delivery's own changes."""

    summary: str
    # The changed file the finding is about, when it is about one.
    path: NotRequired[str]
    # The worker the finding is sent to.
    target: ReviewTarget
    # Briefly, why it goes to that worker.
    reason: str
    # The number of the earlier finding of the same run of review rounds that this one
    # reports again: a correction did not settle it, so it is not sent for another.
    repeats: NotRequired[int]


class ReviewRound(TypedDict):
    """One code review round of a delivery, numbered from 1 across the whole delivery."""

    round: int
    # Empty when the review found nothing worth correcting.
    findings: list[ReviewFinding]
    # The user's instructions the reviewed fixes were made from; absent otherwise.
    instructions: NotRequired[str]
    # A round a pull request comment started: the comment whose text its instructions
    # are. The workflow keeps this reference instead of the text, which the delivery's
    # progress fills in from the mention history.
    mention: NotRequired[dict[str, Any]]


class FindingsAcceptance(TypedDict):
    """The user's acceptance of a delivery pushed with open review findings."""

    accepted_at: str
    accepted_by: str


# Where the code review finds a change's component tests and the stubs of its component
# contract, as the paths of findings name them.
COMPONENT_TESTS_PATH = 'component-tests/'
STUBS_PATH = 'contracts/components/'
CONTRACT_TESTS_PATH = 'tests/'


def correctable(finding: ReviewFinding) -> bool:
    """Whether an author of the delivery can correct the finding: not one about the stubs
    of the component contract, which the routing wrote and the fix has to keep."""
    return not finding.get('path', '').startswith(STUBS_PATH)


def for_worker(findings: list[ReviewFinding], worker: str) -> list[ReviewFinding]:
    """The findings one worker has to correct."""
    return [item for item in findings
            if item['target'] in (worker, 'both') and correctable(item)]


def for_suite(findings: list[ReviewFinding], suite: str) -> list[ReviewFinding]:
    """The findings about the tests of a change with component tests that the author of
    one suite, `contract` or `component`, corrects: those about its files, and those
    about no file, which concern either."""
    other = CONTRACT_TESTS_PATH if suite == 'component' else COMPONENT_TESTS_PATH
    return [item for item in findings if not item.get('path', '').startswith(other)]


def earlier_findings(findings: list[ReviewFinding]) -> list[dict[str, Any]]:
    """The findings a run of review rounds already had corrected, numbered from 1, as the
    next review receives them to tell whether they are settled."""
    return [{'number': number, 'summary': item['summary'][:PREVIOUS_SUMMARY_CHARS],
             'target': item['target'], **({'path': item['path']} if item.get('path') else {})}
            for number, item in enumerate(findings, start=1)]


def review_request(findings: list[ReviewFinding], worker: str, attempt: int,
                   instructions: str | None = None,
                   restore: list[str] | None = None,
                   before_fix: bool = False) -> dict[str, Any]:
    """What an author receives to correct review findings. The test author never sees
    the implementation and the implementation author never sees the tests: only the
    findings' own words reach them. `restore` names repository tests the test author has
    to put back as the repository holds them. `before_fix` marks a review of a change's
    regression tests before the fix, which they still have to fail without."""
    review: dict[str, Any] = {
        'findings': [{key: value for key, value in item.items()
                      if key not in ('target', 'repeats')}
                     for item in for_worker(findings, worker)]}
    if instructions:
        review['instructions'] = instructions
    if restore and worker == 'tests':
        review['restore'] = restore
    if before_fix:
        review['before_fix'] = True
    return {'attempt': attempt, 'review': review}


def commit_findings(findings: list[ReviewFinding]) -> str:
    """The open findings as a commit message lists them: the first COMMIT_FINDINGS and
    the number of all of them."""
    lines = [f'Open review findings: {len(findings)}'
             + (f' (the first {COMMIT_FINDINGS} are listed)' if len(findings) > COMMIT_FINDINGS
                else '')]
    for number, item in enumerate(findings[:COMMIT_FINDINGS], start=1):
        where = f' [{item["path"]}]' if item.get('path') else ''
        summary = ' '.join(item['summary'].split())
        lines.append(f'{number}. ({item["target"]}){where} {summary}')
    return '\n'.join(lines)
