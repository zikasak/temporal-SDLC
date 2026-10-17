"""Bounded code review payloads exchanged between the review and the authors."""
from typing import Any, Literal, NotRequired, TypedDict

ReviewTarget = Literal['implementation', 'tests', 'both']
REVIEW_TARGETS = ('implementation', 'tests', 'both')
# Each run of review rounds, including one started by the user's instructions, reviews
# the delivery at most this often; test repair rounds have a limit of their own.
MAX_REVIEW_ROUNDS = 5
MAX_FINDINGS = 50
SUMMARY_CHARS = 2000
REASON_CHARS = 500
PATH_CHARS = 500
INSTRUCTIONS_CHARS = 10000
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


class ReviewRound(TypedDict):
    """One code review round of a delivery, numbered from 1 across the whole delivery."""

    round: int
    # Empty when the review found nothing worth correcting.
    findings: list[ReviewFinding]
    # The user's instructions the reviewed fixes were made from; absent otherwise.
    instructions: NotRequired[str]


class FindingsAcceptance(TypedDict):
    """The user's acceptance of a delivery pushed with open review findings."""

    accepted_at: str
    accepted_by: str


def for_worker(findings: list[ReviewFinding], worker: str) -> list[ReviewFinding]:
    """The findings one worker has to correct."""
    return [item for item in findings if item['target'] in (worker, 'both')]


def review_request(findings: list[ReviewFinding], worker: str, attempt: int,
                   instructions: str | None = None) -> dict[str, Any]:
    """What an author receives to correct review findings. The test author never sees
    the implementation and the implementation author never sees the tests: only the
    findings' own words reach them."""
    review: dict[str, Any] = {
        'findings': [{key: value for key, value in item.items() if key != 'target'}
                     for item in for_worker(findings, worker)]}
    if instructions:
        review['instructions'] = instructions
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
