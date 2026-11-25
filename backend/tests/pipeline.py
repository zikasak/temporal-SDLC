"""Stand-ins for the delivery stages, shared by the tests of every session that delivers."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from app.config import (
    CODEGEN_TASK_QUEUE, COMPONENT_TEST_GENERATION_TASK_QUEUE, DIAGNOSIS_TASK_QUEUE,
    DOCUMENTATION_TASK_QUEUE,
    IMPLEMENTATION_TASK_QUEUE, REVIEW_TASK_QUEUE, ROUTING_TASK_QUEUE,
    TEST_GENERATION_TASK_QUEUE, VERIFICATION_TASK_QUEUE,
)
from app.delivery.models import DeliveryProgress, DeliveryState, delivery_id
from app.github.models import PUSH_CONFLICT
from app.routing.models import ROUTING_ERROR, SEAM_ERROR
from app.diagnosis.workflows import DiagnosisWorkflow
from app.documentation.workflows import DocumentationWorkflow
from app.implementation.workflows import ImplementationWorkflow
from app.review.workflows import ReviewWorkflow
from app.routing.workflows import ObservabilityWorkflow
from app.test_generation.workflows import ComponentTestGenerationWorkflow
from app.test_generation.workflows import TestGenerationWorkflow as ServiceTestWorkflow
from app.verification.activities import verify_documentation
from app.verification.workflows import VerificationWorkflow

CODE = {"server": {"path": "/generated/server", "files": 8}}
BASELINE_IMAGE = "sha256:" + "e" * 64
REPOSITORY_TESTS_IMAGE = "sha256:" + "f" * 64
ADDED_TESTS = ["com.example.RoomsTest#refusesDoubleBooking", "com.example.OverlapTest#refuses"]
ADDED_COMPONENT_TESTS = ["tests.test_rooms#test_a_merged_room_is_closed"]
# The component tests' runner as their author built it, with the code the delivery started
# from, and as verification builds it with the changed code.
COMPONENT_IMAGE = "sha256:" + "9" * 64
CHANGED_COMPONENT_IMAGE = "sha256:" + "8" * 64
SEAM = {"module": "service.rooms", "symbols": ["Rooms.close"]}
# A seam the code does not have yet, which the change adds first, and the service built
# with it.
ADDED_SEAM = {"module": "service.rooms", "symbols": ["Rooms.close", "Rooms.merged"],
              "added": ["Rooms.merged"]}
SEAM_IMAGE = "sha256:" + "7" * 64
# A page of the web client a `browser` route observes its criterion at, and one with an
# element the page lacks yet, which the change adds first.
PAGE = {"part": "frontend", "entry": "/", "viewports": ["desktop", "narrow"],
        "roles": [{"role": "button", "name": "FR-1"}, {"role": "tooltip"}]}
ADDED_PAGE = {**PAGE, "roles": [*PAGE["roles"], {"testid": "popup"}],
              "added": [{"testid": "popup"}]}
BROKEN_TEST = {"class_name": "RoomsTest", "name": "lists"}
# How long `abandoned` waits in real time for a flow session to close.
ABANDON_POLLS = 400
ABANDON_POLL_SECONDS = 0.05


async def abandoned(handle) -> None:
    """Wait for a flow session that delivered nothing, such as one that expired, failed or
    was finalized with no part started, to end as cancelled. It ends by asking the server
    to cancel it and waiting for that request; awaiting its result on the time-skipping
    server lets that server skip time while the request is on its way, until the run
    times out. So the close is waited for in real time, and only then its result."""
    for _ in range(ABANDON_POLLS):
        if (await handle.describe()).status != WorkflowExecutionStatus.RUNNING:
            break
        await asyncio.sleep(ABANDON_POLL_SECONDS)
    with pytest.raises(WorkflowFailureError) as ended:
        await handle.result()
    assert isinstance(ended.value.cause, CancelledError), repr(ended.value.cause)


async def finished(env, session_id: str) -> DeliveryProgress:
    """The outcome of the session's delivery once its latest run waits for the user's
    next decision; it follows the latest manual retry."""
    handle = env.client.get_workflow_handle(delivery_id(session_id))
    for _ in range(600):
        try:
            state = await handle.query('get_state', result_type=DeliveryState)
        except RPCError as exc:
            # The session has not started its delivery yet.
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
        else:
            if state.waiting:
                return state.progress
        await asyncio.sleep(0.05)
    raise AssertionError("the delivery never finished")


class FakeStages:
    def __init__(self) -> None:
        # Answers of successive reads of the pushed commit's CI checks, the last one
        # repeating: a dict as commit_checks returns it, or an exception to raise.
        self.ci_results: list[Any] = [{'status': 'none', 'checks': []}]
        self.ci_reads: list[str] = []
        # The delivery whose CI the test answers; any other one has no checks.
        self.ci_delivery: str | None = None
        # The comments the delivery wrote in its pull request of its own.
        self.comments: list[str] = []
        self.fail_implementation = False
        self.fail_tests = False
        self.implementation_started = asyncio.Event()
        self.tests_started = asyncio.Event()
        self.release_implementation = asyncio.Event()
        self.release_implementation.set()
        self.release_tests = asyncio.Event()
        self.release_tests.set()
        self.verification_started = asyncio.Event()
        self.release_verification = asyncio.Event()
        self.release_verification.set()
        self.verification_result = 'passed'
        # Outcomes of successive verifications; the last one repeats.
        self.verification_results: list[str] = []
        self.verification_images = None
        self.verified_images: list[tuple[str, str]] = []
        self.verified_languages: list[str] = []
        self.verified_tracked: list[list[str] | None] = []
        # Outcomes of successive baseline runs: 'reproduced' (only the added tests and
        # the corrected existing ones fail), 'not_reproduced' (an added test passes),
        # 'existing_failed', 'skipped', 'left_out' (a test of the suite was not run at
        # all) or 'error'; the last one repeats.
        self.baseline_results = ['reproduced']
        self.baselines: list[tuple[str, str, list[str]]] = []
        # Repository tests a repair of the tests corrects, as that stage reports them.
        self.corrected_tests: list[str] = []
        # The side each successive diagnosis blames, as `tests:component` or
        # `tests:contract` when it names the suite of the tests; the last one repeats.
        self.diagnoses = ['implementation']
        self.diagnosed: list[dict[str, Any]] = []
        self.fail_diagnosis = False
        self.repairs: list[tuple[str, dict[str, Any]]] = []
        # Questions the implementation agent asks before it builds, once.
        self.implementation_questions: list[dict[str, Any]] = []
        # The context of each implementation run and of each test run.
        self.implementation_contexts: list[dict[str, Any]] = []
        self.test_contexts: list[dict[str, Any]] = []
        # Every stage in the order it started.
        self.stages: list[str] = []
        # What had already run when each repository was checked out.
        self.checkouts: list[tuple[str, bool]] = []
        self.turns = 0
        self.fail_checkout = False
        self.published: list[tuple[str, str]] = []
        # What a push of a change with component tests was given, and each pull request
        # step's description of the change.
        self.published_components: dict[str, Any] | None = None
        self.pull_request_changes: list[dict[str, Any]] = []
        self.published_kind: str | None = None
        self.fail_publication = False
        # Successive pushes of the repository whose branch has conflicting commits, each
        # the remote tip as the push error gives it, or None for a push that goes
        # through; the last one repeats. Each push's `resolved` tip is recorded.
        self.push_conflicts: list[str | None] = [None]
        self.resolved: list[str | None] = []
        self.fail_documentation = False
        # Each push of documentation alone, and whether it was pushed as verified.
        self.published_documentation: list[tuple[str, str, bool]] = []
        # What had already run each time the repository's tests were recorded.
        self.snapshots: list[list[str]] = []
        # Outcomes of successive runs of the repository's checks script: 'passed',
        # 'failed', 'missing' or 'error'; the last one repeats. Each run's context, and
        # the excerpts the LLM was asked to analyse.
        self.checks_results: list[str] = ['missing']
        # How many tests each failed run reports failing, other ones each run; unset, a
        # failed run reports only its error line, as runs recorded before counting did.
        self.checks_failures: list[int] = []
        self.checks_runs: list[str] = []
        self.checks_after: list[list[str]] = []
        self.analysed: list[str] = []
        # How often the analysis is refused for the provider's limits before it answers,
        # and the cause it gives.
        self.analysis_limits = 0
        self.analysis_cause = 'code'
        # The review findings each analysis was told, and the conflict it finds with them.
        self.analysed_findings: list[list[dict[str, Any]] | None] = []
        self.analysis_conflict: dict[str, Any] | None = None
        # How many contract tests the repository has.
        self.repository_tests = 3
        self.fail_snapshot = False
        # Repository tests the test author of an API change rewrote, and those of them
        # that still pass, as they were, against the changed service.
        self.rewritten_tests: list[str] = []
        self.still_passing: list[str] = []
        # Those of them that a corrected service passes again, as they were.
        self.passing_after_correction: list[str] = []
        # Those the runner of the repository's tests leaves out of its reports.
        self.not_run: list[str] = []
        self.existing_runs: list[tuple[str, list[str]]] = []
        # The findings of successive code reviews; the last one repeats.
        self.review_findings: list[list[dict[str, Any]]] = [[]]
        self.reviewed: list[dict[str, Any]] = []
        # The same of the reviews of a stage before the fix, by scope: of the seam a
        # change adds, and of its regression tests.
        self.stage_review_findings: dict[str, list[list[dict[str, Any]]]] = {
            'seam': [[]], 'regression': [[]]}
        self.stage_reviewed: dict[str, list[dict[str, Any]]] = {'seam': [], 'regression': []}
        # The stages that had started when each of them ran.
        self.stage_review_after: dict[str, list[list[str]]] = {'seam': [], 'regression': []}
        # The route of each requirement of a brief, by ID, that is not 'contract', for
        # successive routings; the last one repeats.
        self.routes: list[dict[str, str]] = [{}]
        self.fail_routing = False
        self.routed: list[dict[str, Any]] = []
        # Requirements of the brief the first author of a change's tests finds unobservable.
        self.declined: list[str] = []
        # The contexts of the component tests' author, and the requirements it finds
        # unobservable.
        self.component_contexts: list[dict[str, Any]] = []
        self.component_declined: list[str] = []
        self.fail_component_tests = False
        # Outcomes of successive component test runs at the baseline: 'reproduced',
        # 'not_reproduced', 'skipped' or 'error'; and with the change: 'passed', 'failed',
        # 'skipped' or 'error'. The last one of each repeats.
        self.component_baseline_results = ['reproduced']
        self.component_results = ['passed']
        # Why the changed code no longer conforms to the component contract, if it does not.
        self.conformance_error: str | None = None
        # Each component test run: its runner image and the tests it tracked.
        self.component_runs: list[tuple[str, list[str]]] = []
        # The tests the component tests' author adds.
        self.component_added = ADDED_COMPONENT_TESTS
        # The context of each run that adds a seam, whether it fails, and why the code
        # with the seam does not conform to the component contract, if it does not.
        self.seam_contexts: list[dict[str, Any]] = []
        self.fail_seam = False
        # Why the code with the seam does not conform once its author corrected review
        # findings, if it does not; and whether a stage review fails to run.
        self.seam_correction_conformance_error: str | None = None
        self.fail_stage_review = False
        self.seam_conformance_error: str | None = None
        # The service image of each run of the repository's own contract tests.
        self.existing_services: list[str] = []

    @activity.defn(name="checkout_repository")
    async def checkout_repository(self, session_id: str, repository: dict[str, Any]) -> dict[str, Any]:
        if self.fail_checkout:
            raise ApplicationError("clone rejected", non_retryable=True)
        self.checkouts.append((repository["full_name"], self.turns > 0))
        return {"commit": "1" * 40}

    @activity.defn(name="publish_repository")
    async def publish_repository(self, session_id: str, repository: dict[str, Any],
                                 contract: dict[str, Any], kind: str | None,
                                 findings: list[dict[str, Any]] | None = None,
                                 components: dict[str, Any] | None = None,
                                 resolved: str | None = None) -> dict[str, Any]:
        if self.fail_publication:
            raise ApplicationError("remote: Permission denied", non_retryable=True)
        self.resolved.append(resolved)
        conflict = self.push_conflicts[min(len(self.resolved), len(self.push_conflicts)) - 1]
        if conflict is not None and conflict != resolved:
            raise ApplicationError(
                "The branch generated/abc on GitHub has commits this session did not push, "
                "and they conflict with the sources in src/Rooms.java.",
                {"commit": conflict, "branch": "generated/abc", "files": ["src/Rooms.java"],
                 "changes": "+their rooms change"},
                type=PUSH_CONFLICT, non_retryable=True)
        self.published.append((session_id, repository["full_name"]))
        self.contract = contract
        self.published_kind = kind
        self.published_findings = findings
        self.published_components = components
        return {"branch": "generated/abc", "commit": "2" * 40,
                "url": repository["html_url"] + "/tree/generated/abc"}

    @activity.defn(name="commit_checks")
    async def commit_checks(self, full_name: str, commit: str) -> dict[str, Any]:
        if activity.info().workflow_id != self.ci_delivery:
            # Another test's delivery, still waiting on the shared server.
            return {'status': 'none', 'checks': []}
        self.ci_reads.append(commit)
        answer = self.ci_results[min(len(self.ci_reads), len(self.ci_results)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    @activity.defn(name="ci_failure_report")
    async def ci_failure_report(self, full_name: str, commit: str,
                                checks: list[dict[str, Any]]) -> str:
        return "### Backend tests (failure)\nFAILED tests/test_rooms.py::test_lists"

    @activity.defn(name="comment_pull_request")
    async def comment_pull_request(self, full_name: str, number: int, text: str) -> str:
        if activity.info().workflow_id == self.ci_delivery:
            self.comments.append(text)
        return f"https://github.com/{full_name}/pull/{number}#issuecomment-{len(self.comments)}"

    @activity.defn(name="write_pull_request_text")
    async def write_pull_request_text(self, session_id: str, publication: dict[str, Any],
                                      change: dict[str, Any]) -> dict[str, Any]:
        return {"title": "Change", "summary": "What changed."}

    @activity.defn(name="open_pull_request")
    async def open_pull_request(self, session_id: str, repository: dict[str, Any],
                                publication: dict[str, Any], change: dict[str, Any],
                                text: dict[str, Any] | None = None) -> dict[str, Any]:
        self.pull_request_changes.append(change)
        return {"number": 1, "title": "Change", "url": repository["html_url"] + "/pull/1",
                "draft": not publication.get("verified", True), "base": "main"}

    @activity.defn(name="publish_documentation")
    async def publish_documentation(self, session_id: str, repository: dict[str, Any],
                                    language: str, verified: bool) -> dict[str, Any]:
        if self.fail_publication:
            raise ApplicationError("remote: Permission denied", non_retryable=True)
        self.published_documentation.append((session_id, repository["full_name"], verified))
        return {"branch": "generated/abc", "commit": "2" * 40, "verified": verified,
                "url": repository["html_url"] + "/tree/generated/abc"}

    @activity.defn(name="write_documentation")
    async def write_documentation(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        self.stages.append('documentation')
        if self.fail_documentation:
            raise ApplicationError("the author changed nothing", non_retryable=True)
        return {"path": "/documentation/files", "changed_files": ["README.md"],
                "context": context}

    @activity.defn(name="implement_service")
    async def implement_service(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        if 'seam' in context:
            self.stages.append('seam')
            self.seam_contexts.append(context)
            if self.fail_seam:
                raise ApplicationError('The seam agent left no Dockerfile at the root of the '
                                       'repository.', type=SEAM_ERROR, non_retryable=True)
            if self.seam_correction_conformance_error and 'repair' in context:
                return {'session_id': session_id,
                        'conformance_error': self.seam_correction_conformance_error}
            if self.seam_conformance_error:
                return {'session_id': session_id,
                        'conformance_error': self.seam_conformance_error}
            return {'session_id': session_id, 'image_id': SEAM_IMAGE, 'path': '/seam/service'}
        self.stages.append('implementation')
        self.implementation_contexts.append(context)
        if self.implementation_questions and 'answers' not in context:
            return {"session_id": session_id, "questions": self.implementation_questions}
        self.implementation_started.set()
        await self.release_implementation.wait()
        if self.fail_implementation:
            raise ApplicationError("build failed", non_retryable=True)
        if 'repair' in context:
            self.repairs.append(('implementation', context['repair']))
            return {"image_id": "sha256:" + "c" * 64}
        return {"image_id": "sha256:" + "a" * 64, "context": context}

    @activity.defn(name='route_requirements')
    async def route_requirements(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        self.stages.append('routing')
        if self.fail_routing:
            # A string is a reason of the routing's own; otherwise the agent failed.
            raise ApplicationError(
                *((self.fail_routing,) if isinstance(self.fail_routing, str)
                  else ('Provider said: key sk-secret',)),
                type=ROUTING_ERROR if isinstance(self.fail_routing, str) else None,
                non_retryable=True)
        routes = self.routes[min(len(self.routed), len(self.routes) - 1)]
        self.routed.append(context)
        kinds = {'component-added': 'component', 'browser-added': 'browser'}
        details = {'component': {'seam': SEAM}, 'component-added': {'seam': ADDED_SEAM},
                   'browser': {'page': PAGE}, 'browser-added': {'page': ADDED_PAGE}}
        return {'session_id': session_id, 'path': '/routing', 'routes': {
            item['id']: {'route': kinds.get(routes.get(item['id'], 'contract'),
                                            routes.get(item['id'], 'contract')),
                         'reason': f'Route of {item["id"]}.',
                         **details.get(routes.get(item['id'], 'contract'), {})}
            for item in context['brief']['requirements']},
            **({'repository_parts': ['frontend']} if any(
                route.startswith('browser') for route in routes.values()) else {})}

    @activity.defn(name='generate_component_tests')
    async def generate_component_tests(self, session_id: str,
                                       context: dict[str, Any]) -> dict[str, Any]:
        self.stages.append('component-tests')
        self.component_contexts.append(context)
        if self.fail_component_tests:
            raise ApplicationError('ImportError while collecting', non_retryable=True)
        if 'repair' in context:
            self.repairs.append(('component-tests', context['repair']))
        if self.component_declined:
            return {'session_id': session_id, 'unobservable': [
                {'requirement_id': requirement, 'reason': 'Happens in no stubbed symbol.'}
                for requirement in self.component_declined]}
        return {'session_id': session_id, 'image_id': COMPONENT_IMAGE,
                'path': '/component-tests/suite', 'added_tests': self.component_added,
                'changed_tests': [], 'removed_tests': []}

    @activity.defn(name="generate_service_tests")
    async def generate_service_tests(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        self.stages.append('tests')
        self.test_contexts.append(context)
        self.tests_started.set()
        await self.release_tests.wait()
        if self.fail_tests:
            raise ApplicationError("test compilation failed", non_retryable=True)
        if self.declined and 'brief' in context and 'repair' not in context:
            return {'session_id': session_id, 'unobservable': [
                {'requirement_id': requirement, 'reason': 'Needs a pull request.'}
                for requirement in self.declined]}
        changed = {"added_tests": ADDED_TESTS, "changed_tests": [], "removed_tests": []}
        if 'repair' in context:
            self.repairs.append(('tests', context['repair']))
            changed["changed_tests"] = self.corrected_tests
            # Tests put back as the repository holds them are no longer rewritten.
            restore = context['repair'].get('review', {}).get('restore', [])
            self.rewritten_tests = [case for case in self.rewritten_tests if case not in restore]
            # A test a review finding released is rewritten as the finding says.
            self.rewritten_tests += [case for case in context.get('released', [])
                                     if case not in self.rewritten_tests]
        if 'brief' not in context:
            changed = {"added_tests": [], "changed_tests": self.rewritten_tests,
                       "removed_tests": []}
        result = {"image_id": "sha256:" + ("d" if 'repair' in context else "b") * 64,
                  **(changed if 'repository' in context else {})}
        return result if 'repair' in context else {**result, "context": context}

    @activity.defn(name='build_baseline')
    async def build_baseline(self, session_id: str, language: str) -> dict[str, Any]:
        self.stages.append('baseline')
        return {'image_id': BASELINE_IMAGE, 'session_id': session_id}

    @activity.defn(name='snapshot_repository_tests')
    async def snapshot_repository_tests(self, session_id: str, language: str) -> dict[str, Any]:
        if self.fail_snapshot:
            raise ApplicationError('disk full', non_retryable=True)
        self.snapshots.append(list(self.stages))
        return {'tests': self.repository_tests, 'component_tests': 0}

    @activity.defn(name='run_repository_checks')
    async def run_repository_checks(self, session_id: str, language: str) -> dict[str, Any]:
        # Kept apart from `stages`: what had run before each run of the checks.
        self.checks_after.append(list(self.stages))
        status = self.checks_results[min(len(self.checks_runs), len(self.checks_results) - 1)]
        self.checks_runs.append(status)
        if status == 'failed':
            excerpt = 'E   AttributeError: module has no attribute TASK_QUEUE'
            result = {'status': 'failed', 'exit_code': 1, 'excerpt': excerpt,
                      'signature': [excerpt], 'log': '/checks/output.log', 'error': None}
            if self.checks_failures:
                run = self.checks_runs.count('failed') - 1
                failures = self.checks_failures[min(run, len(self.checks_failures) - 1)]
                result.update(failures=failures, signature=[
                    f'tests/test_x.py::test_{run}_{index}' for index in range(failures)])
            return result
        return {'status': status, 'exit_code': 0 if status == 'passed' else None,
                'excerpt': '', 'signature': [], 'log': None,
                'error': 'did not finish' if status == 'error' else None}

    @activity.defn(name='analyze_checks_failure')
    async def analyze_checks_failure(self, session_id: str, excerpt: str,
                                     findings: list[dict[str, Any]] | None = None
                                     ) -> dict[str, Any]:
        if self.analysis_limits:
            self.analysis_limits -= 1
            raise ApplicationError('limits', {'retry_after': 0}, type='ProviderLimitReached',
                                   non_retryable=True)
        self.analysed.append(excerpt)
        self.analysed_findings.append(findings)
        conflict = self.analysis_conflict if findings else None
        return {'cause': self.analysis_cause,
                'summary': 'A test patches a removed attribute.', 'files': ['tests/test_x.py'],
                'instructions': 'Stop patching TASK_QUEUE in tests/test_x.py.',
                'analysed': True, **({'conflict': conflict} if conflict else {})}

    @activity.defn(name='build_repository_tests')
    async def build_repository_tests(self, session_id: str, language: str) -> dict[str, Any]:
        return {'image_id': REPOSITORY_TESTS_IMAGE, 'session_id': session_id}

    @activity.defn(name='build_component_runner')
    async def build_component_runner(self, session_id: str, language: str) -> dict[str, Any]:
        self.stages.append('component-runner')
        if self.conformance_error:
            return {'session_id': session_id, 'conformance_error': self.conformance_error}
        return {'session_id': session_id, 'image_id': CHANGED_COMPONENT_IMAGE}

    @activity.defn(name='run_component_suite')
    async def run_component_suite(self, session_id: str, runner_image: str, language: str,
                                  tracked_tests: list[str]) -> dict[str, Any]:
        baseline = runner_image == COMPONENT_IMAGE
        self.stages.append('component-baseline' if baseline else 'component-verification')
        runs = [image for image, _ in self.component_runs if (image == COMPONENT_IMAGE) == baseline]
        outcomes = self.component_baseline_results if baseline else self.component_results
        outcome = outcomes[min(len(runs), len(outcomes) - 1)]
        self.component_runs.append((runner_image, tracked_tests))
        result = {'session_id': session_id, 'duration_seconds': 1.0, 'details_truncated': False,
                  'service_log': '', 'runner_log': '', 'path': '/component-runs',
                  'test_failures': [], 'test_skips': [], 'missing_tests': [], 'error': None,
                  'tests': 4, 'errors': 0, 'skipped': int(outcome == 'skipped')}
        if outcome == 'error':
            return {**result, 'status': 'error', 'exit_code': None, 'tests': 0, 'passed': 0,
                    'failures': 0, 'skipped': 0, 'tracked': {},
                    'error': 'The component tests timed out or were terminated'}
        failed = (tracked_tests[1:] if outcome == 'not_reproduced'
                  else tracked_tests if baseline or outcome == 'failed' else [])
        if failed and (outcome == 'hung' or not baseline):
            # 'hung': the runner stopped the added tests at its per-test limit.
            message = 'Failed: Timeout >30.0s' if outcome == 'hung' else 'assert False'
            result['test_failures'] = [{'class_name': 'tests.test_rooms',
                                        'name': case.rpartition('#')[2] if outcome == 'hung' else case,
                                        'kind': 'failure', 'message': message}
                                       for case in failed]
        return {**result, 'status': 'failed' if failed else 'passed',
                'exit_code': 1 if failed else 0, 'failures': len(failed),
                'passed': 4 - len(failed) - result['skipped'],
                'tracked': {case: {'tests': 1, 'failed': int(case in failed)}
                            for case in tracked_tests}}

    @activity.defn(name='diagnose_failure')
    async def diagnose_failure(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        if self.fail_diagnosis:
            raise ApplicationError('no verdict', non_retryable=True)
        target, _, suite = self.diagnoses[
            min(len(self.diagnosed), len(self.diagnoses) - 1)].partition(':')
        self.diagnosed.append(context)
        return {'session_id': session_id, 'target': target, 'summary': f'The {target} is wrong.',
                'tests': [BROKEN_TEST] if target == 'tests' else [],
                'instructions': f'Correct the {target}.', 'path': '/diagnosis',
                **({'suite': suite} if suite else {})}

    @activity.defn(name='review_changes')
    async def review_changes(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        scope = context.get('review_scope')
        if scope:
            if self.fail_stage_review:
                raise ApplicationError('the reviewer crashed', non_retryable=True)
            self.stage_review_after[scope].append(list(self.stages))
            listed, reviewed = self.stage_review_findings[scope], self.stage_reviewed[scope]
        else:
            listed, reviewed = self.review_findings, self.reviewed
        findings = listed[min(len(reviewed), len(listed) - 1)]
        reviewed.append(context)
        return {'session_id': session_id, 'findings': findings, 'path': '/review'}

    def _baseline(self, service_image: str, test_image: str, tracked: list[str]) -> dict[str, Any]:
        outcome = self.baseline_results[min(len(self.baselines), len(self.baseline_results) - 1)]
        self.baselines.append((service_image, test_image, tracked))
        if outcome == 'error':
            return {'status': 'error', 'tests': 0, 'passed': 0, 'failures': 0, 'errors': 0,
                    'skipped': 0, 'tracked': {}, 'missing_tests': [],
                    'error': 'The service stopped during verification'}
        passing = tracked[:1] if outcome == 'not_reproduced' else []
        failures = len(tracked) - len(passing) + (outcome == 'existing_failed')
        return {'status': 'failed', 'tests': 9, 'failures': failures,
                'passed': 9 - failures - (outcome == 'skipped'),
                'errors': 0, 'skipped': int(outcome == 'skipped'), 'error': None,
                'missing_tests': ['com.example.RoomsTest#lists'] if outcome == 'left_out' else [],
                'tracked': {case: {'tests': 1, 'failed': int(case not in passing)}
                            for case in tracked},
                'test_failures': [
                    {'class_name': case.partition('#')[0], 'name': case.partition('#')[2],
                     'kind': 'failure', 'message': 'expected 409 but was 201'}
                    for case in tracked if case not in passing]}

    @activity.defn(name='verify_service')
    async def verify_service(self, session_id: str, service_image: str, test_image: str,
                             language: str, tracked_tests: list[str] | None,
                             suite_dir: str) -> dict[str, Any]:
        details = {'session_id': session_id, 'duration_seconds': 1.0, 'exit_code': 0,
                   'test_failures': [], 'details_truncated': False, 'service_log': '',
                   'runner_log': '', 'path': '/verification'}
        if test_image == REPOSITORY_TESTS_IMAGE:
            # The repository's own tests, of which the changed service fails some.
            assert tracked_tests is not None  # each of them is judged on its own
            self.existing_runs.append((suite_dir, tracked_tests))
            self.existing_services.append(service_image)
            passing = self.still_passing + (
                self.passing_after_correction if service_image == "sha256:" + "c" * 64 else [])
            failed = [case for case in tracked_tests
                      if case not in passing and case not in self.not_run]
            return {**details, 'status': 'failed' if failed else 'passed', 'tests': 3,
                    'passed': 3 - len(failed), 'failures': len(failed), 'errors': 0,
                    'skipped': 0, 'error': None, 'missing_tests': [],
                    'tracked': {case: {'tests': int(case not in self.not_run),
                                       'failed': int(case in failed)}
                                for case in tracked_tests}}
        if service_image in (BASELINE_IMAGE, SEAM_IMAGE):
            self.stages.append('baseline-run')
            assert tracked_tests is not None  # a baseline judges each added test
            return {**details, **self._baseline(service_image, test_image, tracked_tests)}
        self.stages.append('verification')
        self.verified_languages.append(language)
        self.verified_tracked.append(tracked_tests)
        self.verification_images = (service_image, test_image)
        self.verified_images.append((service_image, test_image))
        self.verification_started.set()
        await self.release_verification.wait()
        outcome = self.verification_result
        if self.verification_results:
            outcome = self.verification_results[
                min(len(self.verified_images), len(self.verification_results)) - 1]
        if outcome == 'child_error':
            raise ApplicationError('verification worker failed', non_retryable=True)
        failed = outcome == 'failed'
        # 'skipped': the runner is content, but one of its tests did not run.
        skipped = outcome == 'skipped'
        return {**details, 'status': 'passed' if skipped else outcome,
                'tests': 1 + skipped, 'passed': int(outcome in ('passed', 'skipped')),
                'failures': int(failed), 'errors': 0, 'skipped': int(skipped),
                'test_failures': [{'class_name': 'RoomsTest', 'name': 'lists', 'kind': 'failure',
                                   'message': 'Expected 200, got 500'}] if failed else [],
                'test_skips': [{'class_name': 'RoomsTest', 'name': 'books',
                                'message': 'GitHub is not configured'}] if skipped else [],
                'tracked': None if tracked_tests is None else {
                    case: {'tests': 1, 'failed': 0} for case in tracked_tests},
                'missing_tests': None if tracked_tests is None else [],
                'service_log': 'NullPointerException' if failed else '',
                'error': None}


@workflow.defn(name="CodegenWorkflow")
class FakeCodegenWorkflow:
    fail = False
    languages: list[str] = []

    @workflow.run
    async def run(self, session_id: str, document_id: str, language: str
                  ) -> dict[str, Any]:
        FakeCodegenWorkflow.languages.append(language)
        if FakeCodegenWorkflow.fail:
            raise ApplicationError("generator failed", non_retryable=True)
        return CODE


@asynccontextmanager
async def stage_workers(env, stages: FakeStages):
    """A worker per stage queue, each running the real stage workflow over a stand-in."""
    async with Worker(
        # The fake lives in this module, whose imports the sandbox would reject.
        env.client, task_queue=CODEGEN_TASK_QUEUE, workflows=[FakeCodegenWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ), Worker(
        env.client, task_queue=IMPLEMENTATION_TASK_QUEUE,
        workflows=[ImplementationWorkflow], activities=[stages.implement_service],
    ), Worker(
        env.client, task_queue=TEST_GENERATION_TASK_QUEUE,
        workflows=[ServiceTestWorkflow], activities=[stages.generate_service_tests],
    ), Worker(
        env.client, task_queue=VERIFICATION_TASK_QUEUE, workflows=[VerificationWorkflow],
        # The check of documentation is the real one: it starts nothing.
        activities=[stages.verify_service, stages.build_baseline,
                    stages.snapshot_repository_tests, stages.build_repository_tests,
                    stages.build_component_runner, stages.run_component_suite,
                    stages.run_repository_checks, stages.analyze_checks_failure,
                    verify_documentation],
    ), Worker(
        env.client, task_queue=DOCUMENTATION_TASK_QUEUE,
        workflows=[DocumentationWorkflow], activities=[stages.write_documentation],
    ), Worker(
        env.client, task_queue=DIAGNOSIS_TASK_QUEUE,
        workflows=[DiagnosisWorkflow], activities=[stages.diagnose_failure],
    ), Worker(
        env.client, task_queue=REVIEW_TASK_QUEUE,
        workflows=[ReviewWorkflow], activities=[stages.review_changes],
    ), Worker(
        env.client, task_queue=ROUTING_TASK_QUEUE,
        workflows=[ObservabilityWorkflow], activities=[stages.route_requirements],
    ), Worker(
        env.client, task_queue=COMPONENT_TEST_GENERATION_TASK_QUEUE,
        workflows=[ComponentTestGenerationWorkflow],
        activities=[stages.generate_component_tests],
    ):
        FakeCodegenWorkflow.fail = False
        FakeCodegenWorkflow.languages = []
        yield


@contextmanager
def legacy_patch_markers(monkeypatch):
    """Record workflows as the code from before their patches were deprecated did: every
    `workflow.deprecate_patch` call writes the plain marker `workflow.patched` wrote."""
    from temporalio import workflow
    with monkeypatch.context() as legacy:
        legacy.setattr(workflow, 'deprecate_patch', lambda id, **_: workflow.patched(id))
        yield


def patch_ids(history) -> set[str]:
    """The ids of the patch markers `history` holds."""
    import json
    ids = set()
    for event in history.events:
        attributes = event.marker_recorded_event_attributes
        if attributes.marker_name == 'core_patch':
            ids.add(json.loads(attributes.details['patch-data'].payloads[0].data)['id'])
    return ids
