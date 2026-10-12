"""What the delivery pipeline is asked to build and how far it has come."""
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from app.api_design.models import ApiModel
from app.diagnosis.models import RepairRound
from app.github.models import (
    PublicationStatus, RepositoryCheckout, RepositoryPublication, RepositorySelection,
)
from app.requirements.models import Requirement, TaskKind
from app.verification.models import VerificationResult, VerificationStatus

RetryStage = Literal['server', 'tests', 'documentation', 'repository']
RETRY_STAGES = ('server', 'tests', 'documentation', 'repository')
NAMED_TESTS = 5
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
    # What the author of a documentation task wrote: the only stage such a task has.
    documentation: dict[str, Any] | None = None
    documentation_error: str | None = None
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


def judge_baseline(result: VerificationResult, added: list[str], changed: list[str],
                   ) -> tuple[BaselineStatus, str | None]:
    """What a baseline run proves, and what to tell the user when it proves too little.

    `added` are the tests the change added. `changed` are repository tests a repair
    corrected because the change made them wrong; those may fail or pass here."""
    tracked = result['tracked']
    if result['status'] == 'error' or tracked is None:
        return 'error', 'The baseline run could not complete.'
    if result['skipped']:
        # However it came to be skipped, a test that does not run protects nothing.
        return 'error', (f'Tests skipped in the baseline run: {result["skipped"]}. Every '
                         'test of the suite has to run for the baseline to prove anything.')
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


def unproven_green(result: VerificationResult) -> str | None:
    """Why a passed run of a change's suite does not show the change working: a test that
    did not run, such as one skipped by an assumption once the fix is in, passed nothing."""
    if result['skipped']:
        return (f'Tests skipped in the run against the changed service: {result["skipped"]}. '
                'Every test of the suite has to run and pass.')
    if result['missing_tests']:
        return ('Tests of the suite that did not run against the changed service: '
                f'{", ".join(result["missing_tests"][:NAMED_TESTS])}.')
    return None


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
    kept = [case for case in touched if not tracked[case]['failed']]
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
