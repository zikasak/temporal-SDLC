import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from app.config import (
        CODEGEN_TASK_QUEUE, DIAGNOSIS_TASK_QUEUE, IMPLEMENTATION_TASK_QUEUE,
        TEST_GENERATION_TASK_QUEUE, VERIFICATION_TASK_QUEUE,
    )
    from app.delivery.models import DeliveryProgress, DeliveryState
    from app.diagnosis.models import repair_request
    from app.temporal.activities import publish_repository

# Each round is a diagnosis, a repair of one side and a new verification.
MAX_REPAIR_ROUNDS = 5


@workflow.defn
class DeliveryWorkflow:
    """Turns a specified task into verified, published sources: code generation, the
    parallel service and test authors, verification with its repair loop, and the push.
    A stage that fails records its own error instead of failing the run, so it can be
    retried on its own."""

    @workflow.init
    def __init__(self, state: DeliveryState) -> None:
        self.state = state
        self.spec = state.spec
        self.progress = state.progress

    @workflow.run
    async def run(self, state: DeliveryState) -> DeliveryProgress:
        if self.state.retry_stage:
            await self._retry()
        else:
            if self.spec.checkout:
                self.progress.publication_status = 'waiting'
            await self._generate()
        await self._publish()
        return self.progress

    @workflow.query
    def get_state(self) -> DeliveryState:
        return self.state

    async def _generate(self) -> None:
        """Generate code, join the authoring branches, then verify their images."""
        self.progress.verification_status = 'waiting'
        if not self.spec.language:
            self.progress.code_error = "No implementation language was chosen for this session."
            self.progress.verification_status = 'skipped'
            return
        try:
            self.progress.code = await workflow.execute_child_workflow(
                "CodegenWorkflow",
                args=[self.spec.session_id, self.spec.document_id, self.spec.language],
                id=self._child_id('codegen'),
                task_queue=CODEGEN_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning("Code generation failed: %s", exc.cause or exc)
            self.progress.code_error = "The server code could not be generated."
            self.progress.verification_status = 'skipped'
            return
        workflow.logger.info("Server code generated")
        # Each branch records its own failure, so one never cancels the other.
        await asyncio.gather(self._implement_service(), self._generate_tests())
        await self._verify_built()

    def _child_id(self, stage: str, repair: int = 0) -> str:
        child_id = f'{stage}-{self.spec.session_id}'
        if self.state.retry_stage:
            child_id += f'-{workflow.info().run_id}'
        if repair:
            child_id += f'-repair-{repair}'
        return child_id

    async def _retry(self) -> None:
        if self.state.retry_stage == 'repository':
            # Generation and verification already succeeded; only the push is repeated.
            return
        if self.state.retry_stage == 'server' and not self.progress.code:
            # A failed codegen never started either authoring branch.
            await self._generate()
            return
        if self.state.retry_stage == 'server':
            await self._implement_service()
        else:
            await self._generate_tests()
        await self._verify_built()

    async def _verify_built(self) -> None:
        """Verification needs both images; a failed build leaves nothing to verify."""
        if (self.progress.implementation and self.progress.tests
                and not self.progress.implementation_error and not self.progress.tests_error):
            await self._verify_and_repair()
        else:
            self.progress.verification_status = 'skipped'

    def _tests_failed(self) -> bool:
        return bool(self.progress.verification_status == 'completed'
                    and self.progress.verification
                    and self.progress.verification['status'] == 'failed')

    async def _verify_and_repair(self) -> None:
        """Verify; while tests fail, repair the side at fault and verify again."""
        await self._verify_service()
        if not self._tests_failed():
            return
        for attempt in range(1, MAX_REPAIR_ROUNDS + 1):
            if not await self._repair(attempt):
                return
            await self._verify_service(attempt)
            if not self._tests_failed():
                return
        self.progress.verification_error = (
            f'Tests still fail after {MAX_REPAIR_ROUNDS} automatic repair rounds.')

    async def _repair(self, attempt: int) -> bool:
        """Decide which side the failed tests condemn and have its own stage correct it."""
        self.progress.verification_status = 'diagnosing'
        try:
            diagnosis = await workflow.execute_child_workflow(
                'DiagnosisWorkflow',
                args=[self.spec.session_id, {**self._coding_context(),
                                             'verification': self.progress.verification}],
                id=self._child_id('diagnosis', attempt),
                task_queue=DIAGNOSIS_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning('Verification diagnosis failed: %s', exc.cause or exc)
            self.progress.verification_error = 'The failed tests could not be diagnosed.'
            self.progress.verification_status = 'completed'
            return False
        target = diagnosis['target']
        workflow.logger.info('Repair round %d: correcting the %s', attempt, target)
        self.progress.repairs.append(
            {'attempt': attempt, 'target': target, 'summary': diagnosis['summary']})
        self.progress.verification_status = 'repairing'
        repair = repair_request(diagnosis, self.progress.verification, attempt)
        # A failed repair keeps the previous image and records the stage's own error.
        if target == 'implementation':
            await self._implement_service(repair)
        else:
            await self._generate_tests(repair)
        if self.progress.implementation_error or self.progress.tests_error:
            self.progress.verification_status = 'completed'
            return False
        return True

    async def _verify_service(self, repair: int = 0) -> None:
        self.progress.verification_status = 'running'
        try:
            self.progress.verification = await workflow.execute_child_workflow(
                'VerificationWorkflow',
                args=[self.spec.session_id, self.progress.implementation['image_id'],
                      self.progress.tests['image_id'], self.spec.language],
                id=self._child_id('verification', repair),
                task_queue=VERIFICATION_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning('Service verification failed: %s', exc.cause or exc)
            self.progress.verification_error = 'Service verification could not complete.'
            self.progress.verification_status = 'error'
        else:
            self.progress.verification_status = 'completed'

    def _coding_context(self) -> dict[str, Any]:
        return {
            "requirements": self.spec.requirements,
            "requirements_document": self.spec.requirements_document,
            "api_design": self.spec.api,
            "language": self.spec.language,
        }

    def _stage_context(self, repair: dict[str, Any] | None) -> dict[str, Any]:
        context = self._coding_context()
        if self.spec.checkout:
            # The authors then work inside the session's checkout of this repository.
            context['repository'] = self.spec.repository
        if repair:
            context['repair'] = repair
        return context

    async def _publish(self) -> None:
        """Push the sources that passed verification to a new branch of the repository."""
        if not self.spec.checkout:
            return
        if not (self.progress.verification_status == 'completed' and self.progress.verification
                and self.progress.verification['status'] == 'passed'):
            self.progress.publication_status = 'skipped'
            return
        self.progress.publication_status = 'running'
        self.progress.publication_error = None
        try:
            self.progress.publication = await workflow.execute_activity(
                publish_repository,
                args=[self.spec.session_id, self.spec.repository, self.spec.language],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Repository push failed: %s', exc.cause or exc)
            reason = str(getattr(exc.cause, 'message', '') or '')[-500:]
            self.progress.publication_error = (
                'The verified sources could not be pushed to the repository.'
                + (f' {reason}' if reason else ''))
            self.progress.publication_status = 'error'
        else:
            workflow.logger.info('Verified sources pushed to %s',
                                 self.progress.publication['branch'])
            self.progress.publication_status = 'completed'

    async def _implement_service(self, repair: dict[str, Any] | None = None) -> None:
        try:
            self.progress.implementation = await workflow.execute_child_workflow(
                "ImplementationWorkflow",
                args=[self.spec.session_id, self._stage_context(repair)],
                id=self._child_id('implementation', repair['attempt'] if repair else 0),
                task_queue=IMPLEMENTATION_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning("Service implementation failed: %s", exc.cause or exc)
            self.progress.implementation_error = "The service could not be implemented or built."

    async def _generate_tests(self, repair: dict[str, Any] | None = None) -> None:
        try:
            self.progress.tests = await workflow.execute_child_workflow(
                "TestGenerationWorkflow",
                args=[self.spec.session_id, self._stage_context(repair)],
                id=self._child_id('test-generation', repair['attempt'] if repair else 0),
                task_queue=TEST_GENERATION_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning("Service test generation failed: %s", exc.cause or exc)
            self.progress.tests_error = "The service tests could not be generated or built."
