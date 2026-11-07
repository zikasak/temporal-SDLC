"""What the delivery pipeline is asked to build and how far it has come."""
import re
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
from app.verification.models import TestFailure, VerificationResult, VerificationStatus

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
# `prepared`: the code has the seam the change adds before it reproduces anything, and
# conforms to the component contract with it.
SeamStatus = Literal['not_applicable', 'waiting', 'running', 'prepared', 'error']
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
    # The seam a change adds first, with no behaviour change, when a criterion routed to
    # the component tests is observed at symbols the code does not have yet: the stage
    # result, whose `path` and `image_id` are the code with the seam. The baseline runs
    # against it, which proves the seam changed nothing the existing tests check, and the
    # fix starts from it.
    seam: dict[str, Any] | None = None
    seam_error: str | None = None
    seam_status: SeamStatus = 'not_applicable'
    # The review rounds of the seam before any test is written, each followed by its
    # author's correction of their findings, while the seam is `running`.
    seam_reviews: list[ReviewRound] = field(default_factory=list)
    # The run of a change's tests against the service as the repository holds it.
    baseline: VerificationResult | None = None
    baseline_error: str | None = None
    baseline_status: BaselineStatus = 'not_applicable'
    # The review rounds of a change's regression tests once the baseline was reproduced,
    # before the fix: each followed by the authors' correction of their findings and the
    # baseline again.
    test_reviews: list[ReviewRound] = field(default_factory=list)
    # The component tests' runs beside the contract tests': against the code as the
    # repository holds it, judged with `baseline` into `baseline_status`, and against the
    # changed code, which verification runs once the change conforms to its component
    # contract.
    component_baseline: VerificationResult | None = None
    component_verification: VerificationResult | None = None
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


@dataclass(frozen=True)
class SuiteRun:
    """One suite's run of a change: what it is called, what it ran, the tests the change
    added to it and the repository's tests a repair corrected in it."""
    name: str
    result: VerificationResult
    added: list[str]
    changed: list[str] = field(default_factory=list)
    # What a criterion its tests skip on should be rephrased as.
    observable: str = "behaviour the service's HTTP API shows"


def contract_run(result: VerificationResult, added: list[str],
                 changed: list[str]) -> SuiteRun:
    return SuiteRun('contract tests', result, added, changed)


def component_run(result: VerificationResult, added: list[str],
                  changed: list[str] | None = None) -> SuiteRun:
    return SuiteRun('component tests', result, added, changed or [],
                    observable='behaviour a seam of the code shows')


# What the baseline runs against: the repository's code, or that code with the seam the
# change added first.
REPOSITORY_CODE = 'the service as the repository holds it'
SEAM_CODE = 'the code with only the seam the change added'


def judge_baseline(result: VerificationResult | None, added: list[str], changed: list[str],
                   component: SuiteRun | None = None,
                   seam: bool = False) -> tuple[BaselineStatus, str | None]:
    """What a baseline run proves, and what to tell the user when it proves too little.

    `added` are the tests the change added. `changed` are repository tests a repair
    corrected because the change made them wrong; those may fail or pass here. A change
    with component tests is judged on both suites' runs: `component`, and `result`, which
    is None when it has no contract tests to run at all. `seam` is true when the runs were
    against the code with the seam the change added first, which has to change nothing
    the existing tests check. A run whose tests the runner stopped at its time limit
    proves nothing either."""
    runs = [*([contract_run(result, added, changed)] if result is not None else []),
            *([component] if component else [])]
    code = SEAM_CODE if seam else REPOSITORY_CODE
    for run in runs:
        error = _baseline_error(run, prefix=bool(component), code=code)
        if error:
            return 'error', error
    for run in runs:
        error = _hung_tests(run, prefix=bool(component))
        if error:
            return 'error', error
    added_all = [(run, case) for run in runs for case in run.added]
    passing = [case for run, case in added_all
               if run.result['tracked'][case]['failed'] < run.result['tracked'][case]['tests']]
    if passing or not added_all:
        return 'not_reproduced', (
            (f'Added tests that do not fail against {code}: '
             f'{", ".join(passing[:NAMED_TESTS])} ({len(passing)} of {len(added_all)}).'
             if passing else 'No added test is left in the suite.')
            + ' The problem was not reproduced, so nothing was changed.'
            + (' Either the problem is not where the brief puts it, or adding the seam '
               'already changed what the tests observe; retrying the tests adds the seam '
               'again.' if seam and passing else ''))
    return 'reproduced', None


def _baseline_error(run: SuiteRun, prefix: bool, code: str = REPOSITORY_CODE) -> str | None:
    """Why one suite's baseline run proves nothing. `prefix` names the suite, for a change
    that has two, and `code` what the run was against."""
    result, tracked = run.result, run.result['tracked']
    named = f'{run.name[0].upper()}{run.name[1:]}: ' if prefix else ''
    if result['status'] == 'error' or tracked is None:
        reason = (result.get('error') or '').strip().rstrip('.')
        return f'{named}The baseline run could not complete' + (
            f': {reason}.' if reason else '.')
    if result['skipped']:
        # However it came to be skipped, a test that does not run protects nothing. A
        # regression test that skips itself was written for a criterion its suite cannot
        # observe: the routing sent it to the wrong suite, and a new test fares no better.
        return (f'{named}Tests skipped themselves in the baseline run: {result["skipped"]}'
                f'{named_skips(result)}. A test that skips itself was written for '
                f'a criterion the {run.name} cannot observe, so the routing of '
                'that criterion to them was wrong and the baseline proves nothing. '
                f'Rephrase the criterion as {run.observable}, '
                'in a fork of the interview, or write the regression tests again if '
                'the skip has another cause.')
    missing = result['missing_tests']
    if missing:
        return (f'{named}Tests of the suite that did not run in the baseline run: '
                f'{", ".join(missing[:NAMED_TESTS])} ({len(missing)}). Every test '
                'of the suite has to run for the baseline to prove anything.')
    existing = result['failures'] + result['errors'] - sum(
        tracked[case]['failed'] for case in {*run.added, *run.changed})
    if existing:
        return (f'{named}Existing tests failing against {code}: {existing}. The added tests '
                'prove nothing until the existing suite passes there.'
                + (' Adding the seam was to change nothing they check; retrying the tests '
                   'adds it again.' if code == SEAM_CODE else ''))
    return None


# How the Python runner's per-test limit (pytest-timeout, in its Dockerfile) reports a
# test it stopped.
RUNNER_TIMEOUT = re.compile(r'\s*Failed: Timeout >\d')


def hung_tests(result: VerificationResult | None) -> list[TestFailure]:
    """The tests of a run that the runner stopped at its per-test time limit."""
    return [item for item in (result or {}).get('test_failures') or []
            if RUNNER_TIMEOUT.match(item['message'])]


# What the author of a suite whose tests hung in the baseline run is asked to do.
BOUND_INSTRUCTIONS = (
    'The runner stopped the tests in `broken_tests` at its per-test time limit when they ran '
    'against the code without the change: each one waited, with no bound of its own, for '
    'something that code never does. Rewrite only those tests so that every wait for what '
    'the change is to do gives up after a few seconds and fails with an assertion that says '
    'what did not happen, while what each test asserts stays the same. Fakes of '
    'collaborators answer at once and never wait for something the test may not provide.')


def bound_request(hung: list[TestFailure], attempt: int) -> dict[str, Any]:
    """What the author of a suite receives to bound the waits of its `hung` tests: a
    repair that may change those tests alone."""
    return {'attempt': attempt, 'bound': True, 'instructions': BOUND_INSTRUCTIONS,
            'test_failures': hung[:NAMED_TESTS * 6], 'failing_tests': len(hung),
            'broken_tests': [{'class_name': item['class_name'], 'name': item['name']}
                             for item in hung]}


def unreproducing_tests(result: VerificationResult, added: list[str],
                        changed: list[str] | None = None) -> dict[str, list[str]] | None:
    """The tests a change `added` to a suite that its baseline run shows were written
    wrong, and that their author can correct alone, by what is wrong with them:
    `passing`, those that did not fail against the code without the change, so they do
    not reproduce the problem, and `missing`, those the run has no report of. Empty when
    nothing is wrong with them. None when the run proves nothing for a reason beyond
    them, which correcting them would not change: it could not complete, tests skipped
    themselves or hung, existing tests failed, or tests it did not add did not run.
    `changed` are repository tests a repair corrected, which may fail or pass here."""
    if result['status'] == 'error' or result['tracked'] is None:
        return None
    if result['skipped'] or hung_tests(result):
        return None
    tracked = result['tracked']
    existing = result['failures'] + result['errors'] - sum(
        tracked[case]['failed'] for case in {*added, *(changed or [])} if case in tracked)
    missing = list(result['missing_tests'] or [])
    if existing or any(case not in added for case in missing):
        return None
    missing += [case for case in added
                if case not in missing and tracked.get(case, {'tests': 0})['tests'] == 0]
    passing = [case for case in added if case not in missing
               and tracked[case]['failed'] < tracked[case]['tests']]
    return {kind: cases for kind, cases in (('passing', passing), ('missing', missing))
            if cases}


# What the author of a suite whose added tests did not fail in the baseline run is asked
# to do.
REPRODUCE_INSTRUCTIONS = (
    'The tests in `broken_tests` are regression tests you added, and the baseline run '
    'against the code without the change shows them written wrong. Those in '
    '`passing_tests` passed there, so they do not reproduce the problem the brief describes: '
    'each one has to assert the behaviour the brief requires, which that code does not have '
    'yet, and so fail there. Those in `missing_tests` did not run at all, as when the file '
    'is not collected, the test is not discovered, or it does not compile: make each one '
    'run. Rewrite only these tests. Never make one fail by asserting what the brief does '
    'not state, nor by failing unconditionally: it must pass once the problem is fixed.')


def reproduce_request(wrong: dict[str, list[str]], attempt: int) -> dict[str, Any]:
    """What the author of a suite receives to correct the added tests its baseline run
    showed written `wrong` (as `unreproducing_tests` gives them): a repair that may change
    those tests alone."""
    cases = [*wrong.get('passing', []), *wrong.get('missing', [])]
    return {'attempt': attempt, 'reproduce': True, 'instructions': REPRODUCE_INSTRUCTIONS,
            'test_failures': [], 'failing_tests': 0,
            'passing_tests': wrong.get('passing', [])[:NAMED_TESTS * 6],
            'missing_tests': wrong.get('missing', [])[:NAMED_TESTS * 6],
            'broken_tests': [case_ref(case) for case in cases]}


def case_ref(case: str) -> dict[str, str]:
    """A test as reports name it, from its identity (`class_name#name`, as each language's
    `case_id` makes it)."""
    class_name, _, name = case.partition('#')
    return {'class_name': class_name, 'name': name}


def _hung_tests(run: SuiteRun, prefix: bool) -> str | None:
    """Why a baseline run whose tests the runner had to stop proves nothing. Such a test
    waited without a bound of its own for what the code does not do yet: it fails here
    without saying what was missing, costs the run its whole per-test limit, and enough
    of them leave the run no time to finish."""
    hung = [f'{item["class_name"]}#{item["name"]}' for item in hung_tests(run.result)]
    if not hung:
        return None
    named = f'{run.name[0].upper()}{run.name[1:]}: ' if prefix else ''
    return (f'{named}Tests the runner stopped at its per-test time limit in the baseline '
            f'run: {", ".join(hung[:NAMED_TESTS])} ({len(hung)}). A test that waits for '
            'what the change is to do has to give up after a few seconds and fail saying '
            'what did not happen; write the tests again with such a bound.')


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


def unproven_green(result: VerificationResult | None,
                   component: VerificationResult | None = None) -> str | None:
    """Why a passed run of a change's suites does not show the change working: a test that
    did not run, such as one skipped by an assumption once the fix is in, passed nothing.
    `component` is the run of a change's component tests, and `result` is None when the
    change has no contract tests to run."""
    for run, named in ((result, ''), (component, 'Component tests: ')):
        if run is None:
            continue
        if run['skipped']:
            return (f'{named}Tests skipped in the run against the changed service: '
                    f'{run["skipped"]}{named_skips(run)}. Every test of the suite has to run '
                    'and pass: remove the skip, or whatever makes the test skip itself, '
                    'rather than the test.')
        if run['missing_tests']:
            return (f'{named}Tests of the suite that did not run against the changed service: '
                    f'{", ".join(run["missing_tests"][:NAMED_TESTS])}.')
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
    # The criteria a test author found unobservable where they were routed, with its
    # reasons: a retry of the tests routes them again told so, so the new stubs name
    # what the author lacked, such as collaborators the seam calls by name.
    declined_routes: dict[str, Any] | None = None
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