"""Stand-ins for the delivery stages, shared by the tests of every session that delivers."""
import asyncio
from contextlib import asynccontextmanager
from typing import Any

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from app.config import (
    CODEGEN_TASK_QUEUE, DIAGNOSIS_TASK_QUEUE, IMPLEMENTATION_TASK_QUEUE,
    TEST_GENERATION_TASK_QUEUE, VERIFICATION_TASK_QUEUE,
)
from app.diagnosis.workflows import DiagnosisWorkflow
from app.implementation.workflows import ImplementationWorkflow
from app.test_generation.workflows import TestGenerationWorkflow as ServiceTestWorkflow
from app.verification.workflows import VerificationWorkflow

CODE = {"server": {"path": "/generated/server", "files": 8}}
BASELINE_IMAGE = "sha256:" + "e" * 64
ADDED_TESTS = ["com.example.RoomsTest#refusesDoubleBooking", "com.example.OverlapTest#refuses"]
BROKEN_TEST = {"class_name": "RoomsTest", "name": "lists"}


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
        # Every stage in the order it started.
        self.stages: list[str] = []
        # What had already run when each repository was checked out.
        self.checkouts: list[tuple[str, bool]] = []
        self.turns = 0
        self.fail_checkout = False
        self.published: list[tuple[str, str]] = []
        self.published_kind: str | None = None
        self.fail_publication = False

    @activity.defn(name="checkout_repository")
    async def checkout_repository(self, session_id: str, repository: dict[str, Any]) -> dict[str, Any]:
        if self.fail_checkout:
            raise ApplicationError("clone rejected", non_retryable=True)
        self.checkouts.append((repository["full_name"], self.turns > 0))
        return {"commit": "1" * 40}

    @activity.defn(name="publish_repository")
    async def publish_repository(self, session_id: str, repository: dict[str, Any],
                                 contract: dict[str, Any], kind: str | None) -> dict[str, Any]:
        if self.fail_publication:
            raise ApplicationError("remote: Permission denied", non_retryable=True)
        self.published.append((session_id, repository["full_name"]))
        self.contract = contract
        self.published_kind = kind
        return {"branch": "generated/abc", "commit": "2" * 40,
                "url": repository["html_url"] + "/tree/generated/abc"}

    @activity.defn(name="implement_service")
    async def implement_service(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        self.stages.append('implementation')
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
        self.tests_started.set()
        await self.release_tests.wait()
        if self.fail_tests:
            raise ApplicationError("test compilation failed", non_retryable=True)
        changed = {"added_tests": ADDED_TESTS, "changed_tests": [], "removed_tests": []}
        if 'repair' in context:
            self.repairs.append(('tests', context['repair']))
            changed["changed_tests"] = self.corrected_tests
        result = {"image_id": "sha256:" + ("d" if 'repair' in context else "b") * 64,
                  **(changed if 'brief' in context else {})}
        return result if 'repair' in context else {**result, "context": context}

    @activity.defn(name='build_baseline')
    async def build_baseline(self, session_id: str, language: str) -> dict[str, Any]:
        self.stages.append('baseline')
        return {'image_id': BASELINE_IMAGE, 'session_id': session_id}

    @activity.defn(name='diagnose_failure')
    async def diagnose_failure(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        if self.fail_diagnosis:
            raise ApplicationError('no verdict', non_retryable=True)
        target = self.diagnoses[min(len(self.diagnosed), len(self.diagnoses) - 1)]
        self.diagnosed.append(context)
        return {'session_id': session_id, 'target': target, 'summary': f'The {target} is wrong.',
                'tests': [BROKEN_TEST] if target == 'tests' else [],
                'instructions': f'Correct the {target}.', 'path': '/diagnosis'}

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
                             language: str, tracked_tests: list[str] | None) -> dict[str, Any]:
        details = {'session_id': session_id, 'duration_seconds': 1.0, 'exit_code': 0,
                   'test_failures': [], 'details_truncated': False, 'service_log': '',
                   'runner_log': '', 'path': '/verification'}
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
                'tracked': None, 'missing_tests': None, 'service_log': 'NullPointerException' if failed else '',
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
        activities=[stages.verify_service, stages.build_baseline],
    ), Worker(
        env.client, task_queue=DIAGNOSIS_TASK_QUEUE,
        workflows=[DiagnosisWorkflow], activities=[stages.diagnose_failure],
    ):
        FakeCodegenWorkflow.fail = False
        FakeCodegenWorkflow.languages = []
        yield
