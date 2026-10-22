"""Stand-ins for the delivery stages, shared by the tests of every session that delivers."""
import asyncio
from contextlib import asynccontextmanager
from typing import Any

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from app.config import (
    CODEGEN_TASK_QUEUE, DIAGNOSIS_TASK_QUEUE, DOCUMENTATION_TASK_QUEUE,
    IMPLEMENTATION_TASK_QUEUE, REVIEW_TASK_QUEUE, TEST_GENERATION_TASK_QUEUE,
    VERIFICATION_TASK_QUEUE,
)
from app.delivery.models import DeliveryProgress, DeliveryState, delivery_id
from app.diagnosis.workflows import DiagnosisWorkflow
from app.documentation.workflows import DocumentationWorkflow
from app.implementation.workflows import ImplementationWorkflow
from app.review.workflows import ReviewWorkflow
from app.test_generation.workflows import TestGenerationWorkflow as ServiceTestWorkflow
from app.verification.activities import verify_documentation
from app.verification.workflows import VerificationWorkflow

CODE = {"server": {"path": "/generated/server", "files": 8}}
BASELINE_IMAGE = "sha256:" + "e" * 64
REPOSITORY_TESTS_IMAGE = "sha256:" + "f" * 64
ADDED_TESTS = ["com.example.RoomsTest#refusesDoubleBooking", "com.example.OverlapTest#refuses"]
BROKEN_TEST = {"class_name": "RoomsTest", "name": "lists"}


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
        # The side each successive diagnosis blames; the last one repeats.
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
        self.published_kind: str | None = None
        self.fail_publication = False
        self.fail_documentation = False
        # Each push of documentation alone, and whether it was pushed as verified.
        self.published_documentation: list[tuple[str, str, bool]] = []
        # What had already run each time the repository's tests were recorded.
        self.snapshots: list[list[str]] = []
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

    @activity.defn(name="checkout_repository")
    async def checkout_repository(self, session_id: str, repository: dict[str, Any]) -> dict[str, Any]:
        if self.fail_checkout:
            raise ApplicationError("clone rejected", non_retryable=True)
        self.checkouts.append((repository["full_name"], self.turns > 0))
        return {"commit": "1" * 40}

    @activity.defn(name="publish_repository")
    async def publish_repository(self, session_id: str, repository: dict[str, Any],
                                 contract: dict[str, Any], kind: str | None,
                                 findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if self.fail_publication:
            raise ApplicationError("remote: Permission denied", non_retryable=True)
        self.published.append((session_id, repository["full_name"]))
        self.contract = contract
        self.published_kind = kind
        self.published_findings = findings
        return {"branch": "generated/abc", "commit": "2" * 40,
                "url": repository["html_url"] + "/tree/generated/abc"}

    @activity.defn(name="open_pull_request")
    async def open_pull_request(self, session_id: str, repository: dict[str, Any],
                                publication: dict[str, Any], change: dict[str, Any]
                                ) -> dict[str, Any]:
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

    @activity.defn(name="generate_service_tests")
    async def generate_service_tests(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        self.stages.append('tests')
        self.test_contexts.append(context)
        self.tests_started.set()
        await self.release_tests.wait()
        if self.fail_tests:
            raise ApplicationError("test compilation failed", non_retryable=True)
        changed = {"added_tests": ADDED_TESTS, "changed_tests": [], "removed_tests": []}
        if 'repair' in context:
            self.repairs.append(('tests', context['repair']))
            changed["changed_tests"] = self.corrected_tests
            # Tests put back as the repository holds them are no longer rewritten.
            restore = context['repair'].get('review', {}).get('restore', [])
            self.rewritten_tests = [case for case in self.rewritten_tests if case not in restore]
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
        return {'tests': 3}

    @activity.defn(name='build_repository_tests')
    async def build_repository_tests(self, session_id: str, language: str) -> dict[str, Any]:
        return {'image_id': REPOSITORY_TESTS_IMAGE, 'session_id': session_id}

    @activity.defn(name='diagnose_failure')
    async def diagnose_failure(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        if self.fail_diagnosis:
            raise ApplicationError('no verdict', non_retryable=True)
        target = self.diagnoses[min(len(self.diagnosed), len(self.diagnoses) - 1)]
        self.diagnosed.append(context)
        return {'session_id': session_id, 'target': target, 'summary': f'The {target} is wrong.',
                'tests': [BROKEN_TEST] if target == 'tests' else [],
                'instructions': f'Correct the {target}.', 'path': '/diagnosis'}

    @activity.defn(name='review_changes')
    async def review_changes(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        findings = self.review_findings[min(len(self.reviewed), len(self.review_findings) - 1)]
        self.reviewed.append(context)
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
                            for case in tracked}}

    @activity.defn(name='verify_service')
    async def verify_service(self, session_id: str, service_image: str, test_image: str,
                             language: str, tracked_tests: list[str] | None,
                             suite_dir: str) -> dict[str, Any]:
        details = {'session_id': session_id, 'duration_seconds': 1.0, 'exit_code': 0,
                   'test_failures': [], 'details_truncated': False, 'service_log': '',
                   'runner_log': '', 'path': '/verification'}
        if test_image == REPOSITORY_TESTS_IMAGE:
            # The repository's own tests, of which the changed service fails some.
            self.existing_runs.append((suite_dir, tracked_tests))
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
        if service_image == BASELINE_IMAGE:
            self.stages.append('baseline-run')
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
    ):
        FakeCodegenWorkflow.fail = False
        FakeCodegenWorkflow.languages = []
        yield
