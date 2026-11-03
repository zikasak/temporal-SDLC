"""What the delivery pipeline is asked to build and how far it has come."""
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, TypedDict

from app.api_design.models import ApiModel
from app.diagnosis.models import RepairRound
from app.github.models import (
    PublicationStatus, PullRequest, PullRequestMention, RepositoryCheckout,
    RepositoryPublication, RepositorySelection,
)
from app.llm.questions import Question
from app.requirements.models import Requirement, TaskKind
from app.review.models import FindingsAcceptance, ReviewRound
from app.routing.models import RequirementRoute, RoutingStatus
from app.verification.models import VerificationResult, VerificationStatus

# `review` runs a failed code review again on the verified delivery, or goes on from a
# correction of review findings that did not pass verification.
RetryStage = Literal['server', 'tests', 'documentation', 'repository', 'review']
RETRY_STAGES = ('server', 'tests', 'documentation', 'repository', 'review')
NAMED_TESTS = 5
# How long a finished delivery waits for the user's next decision, such as a retry, and
# its sessions with it. Only a closed workflow falls to the namespace's retention.
TASK_IDLE_TIMEOUT = timedelta(days=14)
# How a delivery rejects a mention it already has; handing a mention over is idempotent.
MENTION_HANDLED = 'This comment was already handled.'
# How the tests a change added fared against the service as the repository holds it.
# `reproduced`: they all fail and the existing tests pass, so the change has something to
# turn green. `not_reproduced`: one of them does not fail, so nothing may be fixed.
BaselineStatus = Literal['not_applicable', 'waiting', 'running', 'reproduced', 'not_reproduced',
                         'skipped', 'error']


def delivery_id(session_id: str) -> str:
    """The workflow that delivers a session's task; manual retries are new runs of it."""
    return f'delivery-{session_id}'


class Brief(TypedDict):
    """What a change to an existing service asks for, as its interview recorded it: the
    problem, the expected behaviour and the acceptance criteria."""

    requirements: list[Requirement]
    requirements_document: str
    # Requirements of the repository's contract that the change ends.
    retired: list[Requirement]


class AgentQuestions(TypedDict):
    """Questions a coding agent asked with ask_user. Its run is paused until they are
    answered, and only it ever sees the answers."""

    # The stage workflow that waits for the answers; they are sent to it.
    workflow_id: str
    # Which agent asked: implementation, tests, documentation or diagnosis.
    stage: str
    questions: list[Question]


@dataclass
class TaskSpec:
    """The task one delivery builds, as the session that specified it left it."""

    # Names the artifacts, the repository checkout and the published branch.
    session_id: str
    # The contract: the session's own, or for a change to an existing service the one
    # its repository was published with.
    requirements: list[Requirement]
    requirements_document: str
    api: ApiModel
    # The stored OpenAPI document the server code is generated from; a change that
    # leaves the API as it is generates none.
    document_id: str | None = None
    # What the user said the task is. A bugfix changes the repository's service for its
    # brief and a documentation task its documentation; every other kind is built as a
    # new service.
    kind: TaskKind | None = None
    brief: Brief | None = None
    # Missing when the session expired before a language was chosen.
    language: str | None = None
    repository: RepositorySelection | None = None
    checkout: RepositoryCheckout | None = None
    # An onboarding of the repository's existing code: no code is generated, the contract
    # tests and a root Dockerfile are written and verified against that code, which
    # changes only where the user confirmed a bug in it.
    onboarding: bool = False
    # The task's architecture, as Markdown and in machine-readable form: given to the
    # coding agents and committed with the contract as contracts/architecture.md and
    # contracts/architecture.json. None for a task specified before architectures were.
    architecture: str | None = None
    architecture_structure: dict[str, Any] | None = None
    # An onboarding that adds only the architecture to the repository's contract: nothing
    # is written, verified or reviewed, the two files are pushed with a pull request.
    architecture_only: bool = False


@dataclass
class DeliveryProgress:
    """Each stage's result or error, as the session API reports them."""

    # Where CodegenWorkflow wrote the generated code, once it has run.
    code: dict[str, Any] | None = None
    code_error: str | None = None
    implementation: dict[str, Any] | None = None
    implementation_error: str | None = None
    tests: dict[str, Any] | None = None
    tests_error: str | None = None
    # The component tests of a change's criteria routed to a seam of the code, written
    # beside its contract tests: the stage result, with `added_tests`.
    component_tests: dict[str, Any] | None = None
    component_tests_error: str | None = None
    # What the author of a documentation task wrote: the only stage such a task has.
    documentation: dict[str, Any] | None = None
    documentation_error: str | None = None
    # Which suite can prove each acceptance criterion of a bugfix, by requirement ID,
    # decided before any test is written. A criterion no suite can observe stops the
    # delivery there, with routing_error saying which and why.
    routing: dict[str, RequirementRoute] | None = None
    routing_error: str | None = None
    routing_status: RoutingStatus = 'not_applicable'
    # The run of a change's tests against the service as the repository holds it.
    baseline: VerificationResult | None = None
    baseline_error: str | None = None
    baseline_status: BaselineStatus = 'not_applicable'
    verification: VerificationResult | None = None
    verification_error: str | None = None
    verification_status: VerificationStatus = 'not_applicable'
    # The run of the repository's tests an API change rewrote or removed, as they were,
    # against the changed service.
    existing_tests: VerificationResult | None = None
    # One entry per diagnosed verification failure: which side was sent back for repair.
    repairs: list[RepairRound] = field(default_factory=list)
    # The new branch holding the sources that passed verification, or documentation
    # that no check exists for yet, which the publication then says.
    publication: RepositoryPublication | None = None
    publication_error: str | None = None
    publication_status: PublicationStatus = 'not_applicable'
    # Open questions of the coding agents, which wait for their answers.
    agent_questions: list[AgentQuestions] = field(default_factory=list)
    # Every code review round of the delivery, oldest first.
    reviews: list[ReviewRound] = field(default_factory=list)
    # Why the last code review failed to run; cleared when a review completes.
    review_error: str | None = None
    # Raised when the correction of a review round's findings did not pass verification;
    # retrying the review goes on from that correction.
    correction_failed: bool = False
    # Raised when findings remained after the last review round and the delivery was
    # pushed as unverified; the user accepts it or gives instructions.
    open_findings: bool = False
    findings_acceptance: FindingsAcceptance | None = None
    # The pull request step after the push, with the values of publication_status. Its
    # error never fails the push or the delivery; retrying the repository stage pushes
    # again and retries it. The pull request stays when updating it fails.
    pull_request: PullRequest | None = None
    pull_request_error: str | None = None
    pull_request_status: PublicationStatus = 'not_applicable'
    # Why the LLM could not write the pull request's title and description at the last
    # open or update, so the generic text was used. Never an error of the step; retrying
    # the repository stage has the LLM write them again. Cleared once LLM text is used.
    pull_request_text_error: str | None = None
    # The authorised mentions of the app on the pull request that the delivery still works
    # on, without their text; every mention, with its text, is in the mention history
    # (app.delivery.mentions), and the progress the session API reports shows them all.
    mentions: list[PullRequestMention] = field(default_factory=list)
    # Whether an onboarding changed the repository's existing code, which it does only
    # after the user confirmed a bug in it; the commit and pull request then say so.
    existing_code_changed: bool = False


def judge_baseline(result: VerificationResult, added: list[str], changed: list[str],
                   ) -> tuple[BaselineStatus, str | None]:
    """What a baseline run proves, and what to tell the user when it proves too little.

    `added` are the tests the change added. `changed` are repository tests a repair
    corrected because the change made them wrong; those may fail or pass here."""
    tracked = result['tracked']
    if result['status'] == 'error' or tracked is None:
        return 'error', 'The baseline run could not complete.'
    if result['skipped']:
        # However it came to be skipped, a test that does not run protects nothing. A
        # regression test that skips itself was written for a criterion its suite cannot
        # observe: the routing sent it to the wrong suite, and a new test fares no better.
        return 'error', (f'Tests skipped themselves in the baseline run: {result["skipped"]}'
                         f'{named_skips(result)}. A test that skips itself was written for '
                         'a criterion the contract tests cannot observe, so the routing of '
                         'that criterion to them was wrong and the baseline proves nothing. '
                         'Rephrase the criterion as behaviour the service\'s HTTP API shows, '
                         'in a fork of the interview, or write the regression tests again if '
                         'the skip has another cause.')
    missing = result['missing_tests']
    if missing:
        return 'error', ('Tests of the suite that did not run in the baseline run: '
                         f'{", ".join(missing[:NAMED_TESTS])} ({len(missing)}). Every test '
                         'of the suite has to run for the baseline to prove anything.')
    existing = result['failures'] + result['errors'] - sum(
        tracked[case]['failed'] for case in {*added, *changed})
    if existing:
        return 'error', ('Existing tests failing against the service as the repository holds '
                         f'it: {existing}. The added tests prove nothing until the existing '
                         'suite passes there.')
    passing = [case for case in added if tracked[case]['failed'] < tracked[case]['tests']]
    if passing or not added:
        return 'not_reproduced', (
            (f'Added tests that do not fail against the service as the repository holds it: '
             f'{", ".join(passing[:NAMED_TESTS])} ({len(passing)} of {len(added)}).'
             if passing else 'No added test is left in the suite.')
            + ' The problem was not reproduced, so nothing was changed.')
    return 'reproduced', None


SKIP_REASON_CHARS = 300


def named_skips(result: VerificationResult) -> str:
    """The first skipped tests of `result` with the reasons their reports give, for the
    author who has to make them run; empty for a result recorded without them."""
    skips = result.get('test_skips') or []
    if not skips:
        return ''
    named = '; '.join(
        f'{item["class_name"]}#{item["name"]}'
        + (f' (skip reason: {" ".join(item["message"].split())[:SKIP_REASON_CHARS]})'
           if item['message'].strip() else '')
        for item in skips[:NAMED_TESTS])
    more = len(skips) - NAMED_TESTS
    return f' ({named}{f"; {more} more" if more > 0 else ""})'


def unproven_green(result: VerificationResult) -> str | None:
    """Why a passed run of a change's suite does not show the change working: a test that
    did not run, such as one skipped by an assumption once the fix is in, passed nothing."""
    if result['skipped']:
        return (f'Tests skipped in the run against the changed service: {result["skipped"]}'
                f'{named_skips(result)}. Every test of the suite has to run and pass: remove '
                'the skip, or whatever makes the test skip itself, rather than the test.')
    if result['missing_tests']:
        return ('Tests of the suite that did not run against the changed service: '
                f'{", ".join(result["missing_tests"][:NAMED_TESTS])}.')
    return None


def kept_tests(result: VerificationResult, touched: list[str]) -> list[str]:
    """The repository's tests in `touched`, changed or removed by an API change, that ran
    as they were against the changed service, in `result`, and passed there."""
    tracked = result['tracked']
    if result['status'] == 'error' or tracked is None:
        return []
    return [case for case in touched
            if tracked.get(case, {}).get('tests') and not tracked[case]['failed']]


def restorable_tests(progress: 'DeliveryProgress') -> list[str]:
    """The repository's tests an API change changed or removed that its passed suite has to
    put back as the repository holds them: all of them ran, as they were, against the
    changed service, and these still passed there. Empty when verification did not pass,
    or when some did not run or could not be run, which no restore settles."""
    result, tests, verification = (progress.existing_tests, progress.tests,
                                   progress.verification)
    if not result or not tests or not verification or verification['status'] != 'passed':
        return []
    touched = [*tests.get('changed_tests', []), *tests.get('removed_tests', [])]
    tracked = result['tracked']
    if result['status'] == 'error' or tracked is None or any(
            not tracked.get(case, {}).get('tests') for case in touched):
        return []
    return kept_tests(result, touched)


def judge_kept(result: VerificationResult, touched: list[str]) -> str | None:
    """Why an API change may not publish its suite: `touched` are the repository's tests
    it changed or removed, and `result` their run, as they were, against the changed
    service. One that still passes there is still supported by the new contract."""
    tracked = result['tracked']
    if result['status'] == 'error' or tracked is None:
        return "The repository's tests could not be run against the changed service."
    unrun = [case for case in touched if not tracked[case]['tests']]
    if unrun:
        # A test that did not run was not seen to fail, so nothing allows its change.
        return ('Tests of the repository that were changed or removed did not run, as they '
                f'were, against the changed service: {", ".join(unrun[:NAMED_TESTS])} '
                f'({len(unrun)}). Nothing shows that the new contract no longer supports them.')
    kept = kept_tests(result, touched)
    if not kept:
        return None
    return ('Tests of the repository that the changed service still passes were changed or '
            f'removed: {", ".join(kept[:NAMED_TESTS])} ({len(kept)}). A test the new contract '
            'still supports has to stay as it is.')


@dataclass
class DeliveryState:
    """Workflow input and query result; a manual retry starts a new run from it."""

    spec: TaskSpec
    progress: DeliveryProgress = field(default_factory=DeliveryProgress)
    retry_stage: RetryStage | None = None
    # The user's instructions about open review findings, which start a new run of fix
    # rounds, verification and code review on the delivered sources.
    instructions: str | None = None
    # A run that only records the progress as it is given, such as the user's acceptance
    # of open review findings.
    record_only: bool = False
    # The comment ID of the pull request mention whose fix request this run carries out,
    # with its text as the instructions, and its comment type: conversation and inline
    # review comments are numbered apart, so only both identify a comment. A state from
    # before the type was recorded has none.
    mention: int | None = None
    mention_type: str | None = None
    # The mentions waiting for their round, in the order they arrived, each as
    # [comment type, comment ID]; a state from before has bare comment IDs.
    mention_queue: list[Any] = field(default_factory=list)
    # The instructions of a requested documentation fix whose run ended without a push,
    # kept apart from the mention bookkeeping: retrying the documentation stage carries
    # out that fix again instead of the original task. Cleared once documentation is
    # written and checked.
    documentation_fix: str | None = None
    # The fix of a code delivery from the user's instructions or a mention's comment,
    # kept from when its round starts until its correction passed verification: its
    # instructions, the open findings it came with, its review round number and, for a
    # mention, the comment it refers to. Retrying the server or tests stage after it
    # failed carries out that fix again on the delivered sources, not the original task.
    code_fix: dict[str, Any] | None = None
    # The repository's tests that retrying the tests stage of an API change has its test
    # author put back as the repository holds them, told why: those its rejected suite
    # changed or removed that the changed service still passed. Set by the retry.
    restore_tests: list[str] = field(default_factory=list)
    # The repository's tests an API change was seen to keep supporting: each ran, as the
    # repository holds it, against the changed service and passed. Every later author of
    # the delivery, a repair included, has to leave them as the repository holds them.
    kept_tests: list[str] = field(default_factory=list)
    # Raised once the run has done its work and waits for the user's next decision; a
    # restart continues the delivery as a new run of the same workflow.
    waiting: bool = False


def mention_key(item: Any) -> tuple[str | None, int]:
    """The identity of a queued mention: its comment type, None for a bare comment ID
    from a state recorded before the type was, and its comment ID."""
    if isinstance(item, (list, tuple)):
        return item[0], int(item[1])
    return None, int(item)


def is_mention(entry: dict[str, Any], key: tuple[str | None, int]) -> bool:
    comment_type, comment_id = key
    return entry['comment_id'] == comment_id and (
        comment_type is None or entry['comment_type'] == comment_type)


STOPPED = 'The delivery was stopped before this stage finished.'
# Marks a mention whose fix round was stopped: the next run of the delivery replies in
# the pull request with that outcome before the mention is settled.
OUTCOME_OWED = 'outcome_owed'


def interrupted(state: DeliveryState) -> DeliveryState:
    """A run stopped from outside, such as a terminated one, as a retry finds it: the
    stage it was in has failed, so the retry of that stage goes on from there."""
    progress = state.progress
    state.waiting = False
    progress.agent_questions = []
    for mention in progress.mentions:
        if mention['status'] in ('classifying', 'running'):
            if mention['status'] == 'running':
                # A reviewer was told its fix round started and is owed its outcome.
                mention[OUTCOME_OWED] = True
            mention['status'] = 'error'
            mention['error'] = STOPPED
    if state.mention is not None:
        if state.spec.kind == 'docs' and state.instructions:
            # The stopped fix is what retrying the documentation stage carries out.
            state.documentation_fix = state.instructions
        state.mention = None
        state.mention_type = None
        state.instructions = None
    if progress.publication_status == 'running':
        progress.publication_error = STOPPED
        progress.publication_status = 'error'
        progress.pull_request_status = 'skipped'
    elif progress.pull_request_status == 'running':
        progress.pull_request_error = STOPPED
        progress.pull_request_status = 'error'
    elif progress.verification_status == 'reviewing':
        progress.review_error = STOPPED
        progress.verification_status = 'error'
    elif progress.verification_status == 'repairing' and progress.reviews:
        # A correction of review findings; retrying the review goes on from it.
        progress.correction_failed = True
        progress.verification_status = 'error'
    elif progress.routing_status == 'running':
        progress.routing_error = STOPPED
        progress.routing_status = 'error'
        progress.baseline_status = 'skipped'
        progress.verification_status = 'skipped'
    elif progress.verification_status in ('waiting', 'running', 'diagnosing', 'repairing'):
        # Before the delivery was verified, the author whose retry starts it over failed.
        if state.spec.kind == 'docs':
            progress.documentation_error = STOPPED
        elif state.spec.kind == 'bugfix':
            progress.tests_error = STOPPED
        elif state.spec.onboarding:
            progress.implementation_error = STOPPED
        elif not progress.code:
            progress.code_error = STOPPED
        else:
            progress.implementation_error = STOPPED
        progress.verification_status = 'error'
    return state


def open_review_findings(progress: DeliveryProgress) -> list:
    """The findings of the last review round: those still open after it."""
    return list(progress.reviews[-1]['findings']) if progress.reviews else []