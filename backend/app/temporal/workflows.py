import asyncio
import json
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from app.api_design.models import ApiDesignState
    from app.api_design.openapi import api_problems
    from app.api_design.prompt import OPENING_FALLBACK
    from app.config import (
        CODEGEN_TASK_QUEUE, DIAGNOSIS_TASK_QUEUE, IMPLEMENTATION_TASK_QUEUE,
        TEST_GENERATION_TASK_QUEUE, VERIFICATION_TASK_QUEUE,
    )
    from app.diagnosis.models import repair_request
    from app.github.models import RepositorySelection
    from app.requirements.models import RequirementsState
    from app.requirements.prompt import OPENING_MESSAGE
    from app.temporal.activities import (
        api_design_turn,
        checkout_repository,
        greet,
        parse_document,
        publish_repository,
        requirements_turn,
        send_llm_message,
        write_openapi_document,
        write_requirements_document,
    )

SESSION_IDLE_TIMEOUT = timedelta(hours=24)
# How long a Chat message may wait for its session's repository.
REPOSITORY_CHECKOUT_TIMEOUT = timedelta(minutes=10)
MAX_TRANSCRIPT_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 10000
# Each round is a diagnosis, a repair of one side and a new verification.
MAX_REPAIR_ROUNDS = 5
OPEN_STATUSES = ("interviewing", "turn_running")


def _await_checkout(state: RequirementsState | ApiDesignState) -> str:
    """Hold a new session in `preparing` until its repository is checked out, so no
    message reaches the model first. Returns the status to continue with."""
    status = state.status
    if state.repository and not state.checkout and status in OPEN_STATUSES:
        state.status = "preparing"
    return status


async def _check_out(state: RequirementsState | ApiDesignState, resume_status: str) -> bool:
    """Check out the repository of a `preparing` session; False when it cannot go on."""
    if state.status != "preparing":
        return True
    # Histories from before checkouts existed replay without one.
    if workflow.patched("repository-checkout"):
        try:
            state.checkout = await workflow.execute_activity(
                checkout_repository,
                args=[workflow.info().workflow_id, state.repository],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning("Repository checkout failed: %s", exc.cause or exc)
            state.status = "failed"
            state.error = ("The repository could not be checked out. Check the GitHub "
                           "connection and start a new session.")
            return False
    state.status = resume_status
    return True


@workflow.defn
class SayHelloWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            greet,
            name,
            schedule_to_close_timeout=timedelta(seconds=10),
        )


@workflow.defn
class LLMRequestWorkflow:
    @workflow.run
    async def run(
        self, messages: Any, parse_document_id: str | None = None,
        session_id: str | None = None, repository: RepositorySelection | None = None,
    ) -> str:
        workflow.logger.info("LLM request started (session %s, parse document %s)",
                             session_id, parse_document_id)
        if repository:
            # Only a session's first message clones; later ones find the working copy.
            try:
                await workflow.execute_activity(
                    checkout_repository,
                    args=[session_id or workflow.info().workflow_id, repository],
                    start_to_close_timeout=REPOSITORY_CHECKOUT_TIMEOUT,
                    schedule_to_close_timeout=REPOSITORY_CHECKOUT_TIMEOUT,
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError as exc:
                # The model still answers, and tells the user what it could not read.
                workflow.logger.warning("Repository checkout failed: %s", exc.cause or exc)
        if parse_document_id:
            await workflow.execute_activity(
                parse_document,
                parse_document_id,
                start_to_close_timeout=timedelta(seconds=90),
                schedule_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        return await workflow.execute_activity(
            send_llm_message,
            args=[messages, session_id, repository],
            start_to_close_timeout=timedelta(minutes=6),
            schedule_to_close_timeout=timedelta(minutes=7),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class RequirementsWorkflow:
    """One requirements interview: each user message is an update, and the
    finalize update turns the gathered requirements into a document."""

    @workflow.init
    def __init__(self, state: RequirementsState | None = None) -> None:
        self.state = state or RequirementsState(
            messages=[{"role": "assistant", "content": OPENING_MESSAGE}]
        )
        # A session forked after a user message starts by answering it.
        if self.state.messages[-1]["role"] == "user":
            self.state.status = "turn_running"
        self._resume_status = _await_checkout(self.state)
        self._updates = 0
        # Validators can all pass in one workflow task, so turns queue here.
        self._turn_lock = asyncio.Lock()

    @workflow.run
    async def run(self, state: RequirementsState | None = None) -> dict[str, Any] | None:
        ready = await _check_out(self.state, self._resume_status)
        if ready and self.state.messages[-1]["role"] == "user":
            async with self._turn_lock:
                await self._turn()
        while self.state.status not in {"completed", "failed"}:
            seen = self._updates
            try:
                await workflow.wait_condition(
                    lambda: self._updates != seen
                    or self.state.status in {"completed", "failed"}
                    or workflow.info().is_continue_as_new_suggested(),
                    timeout=SESSION_IDLE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                if self.state.status == "interviewing":
                    await self._expire()
                continue
            if workflow.info().is_continue_as_new_suggested():
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.logger.info("Continuing as new")
                workflow.continue_as_new(self.state)
        await workflow.wait_condition(workflow.all_handlers_finished)
        return self.state.document

    @workflow.query
    def get_state(self) -> RequirementsState:
        return self.state

    @workflow.update
    async def send_message(self, message: str) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError("This session is no longer accepting messages.")
            workflow.logger.info("Message received (%d characters)", len(message))
            self.state.messages.append({"role": "user", "content": message.strip()})
            await self._turn()

    @send_message.validator
    def validate_send_message(self, message: str) -> None:
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before sending another.")
        text = message.strip()
        if not text or len(text) > MAX_MESSAGE_CHARS:
            raise ValueError(f"A message must contain 1 to {MAX_MESSAGE_CHARS} characters.")
        encoded = json.dumps(
            [*self.state.messages, {"role": "user", "content": text}],
            ensure_ascii=False, separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > MAX_TRANSCRIPT_BYTES:
            raise ValueError("This interview is too long to continue. Generate the document.")

    @workflow.update
    async def retry_message(self) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing" or self.state.messages[-1]["role"] != "user":
                raise ApplicationError("There is no unanswered message to retry.")
            await self._turn()

    @retry_message.validator
    def validate_retry_message(self) -> None:
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] != "user":
            raise ValueError("There is no unanswered message to retry.")

    @workflow.update
    async def finalize(self) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError("This session is no longer accepting changes.")
            workflow.logger.info("Finalize requested")
            await self._write_document()

    @finalize.validator
    def validate_finalize(self) -> None:
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before generating the document.")
        if not self.state.requirements:
            raise ValueError("No requirements have been recorded yet.")

    async def _turn(self) -> bool:
        self.state.status = "turn_running"
        self.state.error = None
        try:
            result = await workflow.execute_activity(
                requirements_turn,
                args=[self.state.messages, self.state.requirements],
                start_to_close_timeout=timedelta(minutes=6),
                schedule_to_close_timeout=timedelta(minutes=7),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as exc:
            workflow.logger.warning("Requirements turn failed: %s", exc.cause or exc)
            self.state.error = "The LLM could not answer. Retry this message."
            ok = False
        else:
            self.state.messages.append({"role": "assistant", "content": result["reply"]})
            self.state.requirements = result["requirements"]
            ok = True
        self.state.status = "interviewing"
        return ok

    async def _write_document(self) -> bool:
        self.state.status = "finalizing"
        self.state.error = None
        try:
            self.state.document = await workflow.execute_activity(
                write_requirements_document,
                args=[workflow.info().workflow_id, self.state.messages, self.state.requirements],
                start_to_close_timeout=timedelta(minutes=6),
                schedule_to_close_timeout=timedelta(minutes=13),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning("Requirements document failed: %s", exc.cause or exc)
            self.state.status = "interviewing"
            self.state.error = "The document could not be generated. Try again."
            return False
        workflow.logger.info("Requirements document written")
        self.state.status = "completed"
        return True

    async def _expire(self) -> None:
        """After a day without messages, keep whatever was gathered."""
        workflow.logger.info("Requirements session idle, expiring")
        if self.state.requirements and await self._write_document():
            return
        self.state.status = "failed"
        self.state.error = (
            "The session expired before any requirements were recorded."
            if not self.state.requirements
            else "The session expired and the document could not be generated."
        )


@workflow.defn
class ApiDesignWorkflow:
    """One API design discussion based on a finished requirements session: the
    model drafts the API first, each user message is an update, and the
    finalize update renders the API model as an OpenAPI document."""

    @workflow.init
    def __init__(self, state: ApiDesignState) -> None:
        self.state = state
        # A new session starts with the draft; one forked after a user message
        # starts by answering it.
        if not self.state.generation_retry_stage and (
            not self.state.messages or self.state.messages[-1]["role"] == "user"
        ):
            self.state.status = "turn_running"
        self._resume_status = _await_checkout(self.state)
        self._updates = 0
        self._turn_lock = asyncio.Lock()

    @workflow.run
    async def run(self, state: ApiDesignState) -> dict[str, Any] | None:
        if self.state.generation_retry_stage:
            await self._retry_generation()
            await self._publish()
            return self.state.document
        ready = await _check_out(self.state, self._resume_status)
        if ready and not self.state.messages:
            async with self._turn_lock:
                if not await self._turn():
                    self.state.messages.append(
                        {"role": "assistant", "content": OPENING_FALLBACK}
                    )
                    self.state.error = None
        elif ready and self.state.messages[-1]["role"] == "user":
            async with self._turn_lock:
                await self._turn()
        while self.state.status not in {"completed", "failed"}:
            seen = self._updates
            try:
                await workflow.wait_condition(
                    lambda: self._updates != seen
                    or self.state.status in {"completed", "failed"}
                    or workflow.info().is_continue_as_new_suggested(),
                    timeout=SESSION_IDLE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                if self.state.status == "interviewing":
                    await self._expire()
                continue
            if workflow.info().is_continue_as_new_suggested():
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.logger.info("Continuing as new")
                workflow.continue_as_new(self.state)
        await workflow.wait_condition(workflow.all_handlers_finished)
        if self.state.status == "completed" and self.state.document:
            if self.state.checkout:
                self.state.publication_status = 'waiting'
            await self._generate_code()
            await self._publish()
        return self.state.document

    @workflow.query
    def get_state(self) -> ApiDesignState:
        return self.state

    @workflow.update
    async def send_message(self, message: str) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError("This session is no longer accepting messages.")
            workflow.logger.info("Message received (%d characters)", len(message))
            self.state.messages.append({"role": "user", "content": message.strip()})
            await self._turn()

    @send_message.validator
    def validate_send_message(self, message: str) -> None:
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before sending another.")
        text = message.strip()
        if not text or len(text) > MAX_MESSAGE_CHARS:
            raise ValueError(f"A message must contain 1 to {MAX_MESSAGE_CHARS} characters.")
        encoded = json.dumps(
            [*self.state.messages, {"role": "user", "content": text}],
            ensure_ascii=False, separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > MAX_TRANSCRIPT_BYTES:
            raise ValueError("This discussion is too long to continue. Generate the OpenAPI.")

    @workflow.update
    async def retry_message(self) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing" or not self.state.messages or self.state.messages[-1]["role"] != "user":
                raise ApplicationError("There is no unanswered message to retry.")
            await self._turn()

    @retry_message.validator
    def validate_retry_message(self) -> None:
        if self.state.status != "interviewing" or not self.state.messages or self.state.messages[-1]["role"] != "user":
            raise ValueError("There is no unanswered message to retry.")

    @workflow.update
    async def finalize(self) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError("This session is no longer accepting changes.")
            workflow.logger.info("Finalize requested")
            await self._write_document()

    @finalize.validator
    def validate_finalize(self) -> None:
        if self.state.status != "interviewing" or (self.state.messages and self.state.messages[-1]["role"] == "user"):
            raise ValueError("Retry the unanswered message before generating the OpenAPI.")
        problems = api_problems(self.state.api)
        if problems:
            raise ValueError("Fix these problems first: " + " ".join(problems))

    async def _turn(self) -> bool:
        self.state.status = "turn_running"
        self.state.error = None
        try:
            result = await workflow.execute_activity(
                api_design_turn,
                self.state,
                start_to_close_timeout=timedelta(minutes=8),
                schedule_to_close_timeout=timedelta(minutes=9),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as exc:
            workflow.logger.warning("API design turn failed: %s", exc.cause or exc)
            self.state.error = "The LLM could not answer. Retry this message."
            ok = False
        else:
            self.state.messages.append({"role": "assistant", "content": result["reply"]})
            self.state.api = result["api"]
            ok = True
        self.state.status = "interviewing"
        return ok

    async def _write_document(self) -> bool:
        self.state.status = "finalizing"
        self.state.error = None
        try:
            self.state.document = await workflow.execute_activity(
                write_openapi_document,
                args=[workflow.info().workflow_id, self.state.api],
                start_to_close_timeout=timedelta(minutes=1),
                schedule_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning("OpenAPI document failed: %s", exc.cause or exc)
            self.state.status = "interviewing"
            self.state.error = "The OpenAPI document could not be generated. Try again."
            return False
        workflow.logger.info("OpenAPI document written")
        self.state.status = "completed"
        return True

    async def _generate_code(self) -> None:
        """Generate code, join the authoring branches, then verify their images."""
        verification_enabled = workflow.patched('verification-child-workflow')
        if verification_enabled:
            self.state.verification_status = 'waiting'
        # Sessions that finished before code generation existed must replay
        # without the child workflow.
        if not workflow.patched("codegen-child-workflow"):
            self.state.code_error = "This session finished before code generation was added."
            if verification_enabled:
                self.state.verification_status = 'skipped'
            return
        session_id = workflow.info().workflow_id
        try:
            self.state.code = await workflow.execute_child_workflow(
                "CodegenWorkflow",
                args=[session_id, self.state.document["document_id"]],
                id=self._generation_child_id('codegen', session_id),
                task_queue=CODEGEN_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning("Code generation failed: %s", exc.cause or exc)
            self.state.code_error = "The server code could not be generated."
            if verification_enabled:
                self.state.verification_status = 'skipped'
            return
        workflow.logger.info("Server code generated")
        if workflow.patched("implementation-child-workflow"):
            if workflow.patched("parallel-test-generation"):
                # Each branch records its own failure, so one never cancels the other.
                await asyncio.gather(
                    self._implement_service(session_id), self._generate_tests(session_id),
                )
            else:
                # Histories from before test generation retain the original child order.
                await self._implement_service(session_id)
        if verification_enabled:
            if (self.state.implementation and self.state.tests
                    and not self.state.implementation_error and not self.state.tests_error):
                await self._verify_and_repair(session_id)
            else:
                self.state.verification_status = 'skipped'

    def _generation_child_id(self, stage: str, session_id: str, repair: int = 0) -> str:
        child_id = f'{stage}-{session_id}'
        if self.state.generation_retry_stage:
            child_id += f'-{workflow.info().run_id}'
        if repair:
            child_id += f'-repair-{repair}'
        return child_id

    async def _retry_generation(self) -> None:
        session_id = workflow.info().workflow_id
        if self.state.generation_retry_stage == 'repository':
            # Generation and verification already succeeded; only the push is repeated.
            return
        if self.state.generation_retry_stage == 'server' and not self.state.code:
            # A failed codegen never started either authoring branch.
            await self._generate_code()
            return
        if self.state.generation_retry_stage == 'server':
            await self._implement_service(session_id)
        else:
            await self._generate_tests(session_id)
        if (self.state.implementation and self.state.tests
                and not self.state.implementation_error and not self.state.tests_error):
            await self._verify_and_repair(session_id)
        else:
            self.state.verification_status = 'skipped'

    def _tests_failed(self) -> bool:
        return bool(self.state.verification_status == 'completed' and self.state.verification
                    and self.state.verification['status'] == 'failed')

    async def _verify_and_repair(self, session_id: str) -> None:
        """Verify; while tests fail, repair the side at fault and verify again."""
        await self._verify_service(session_id)
        # Histories from before the repair loop end with their first verification.
        if not self._tests_failed() or not workflow.patched('verification-repair-loop'):
            return
        for attempt in range(1, MAX_REPAIR_ROUNDS + 1):
            if not await self._repair(session_id, attempt):
                return
            await self._verify_service(session_id, attempt)
            if not self._tests_failed():
                return
        self.state.verification_error = (
            f'Tests still fail after {MAX_REPAIR_ROUNDS} automatic repair rounds.')

    async def _repair(self, session_id: str, attempt: int) -> bool:
        """Decide which side the failed tests condemn and have its own stage correct it."""
        self.state.verification_status = 'diagnosing'
        try:
            diagnosis = await workflow.execute_child_workflow(
                'DiagnosisWorkflow',
                args=[session_id, {**self._coding_context(),
                                   'verification': self.state.verification}],
                id=self._generation_child_id('diagnosis', session_id, attempt),
                task_queue=DIAGNOSIS_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning('Verification diagnosis failed: %s', exc.cause or exc)
            self.state.verification_error = 'The failed tests could not be diagnosed.'
            self.state.verification_status = 'completed'
            return False
        target = diagnosis['target']
        workflow.logger.info('Repair round %d: correcting the %s', attempt, target)
        self.state.repairs.append(
            {'attempt': attempt, 'target': target, 'summary': diagnosis['summary']})
        self.state.verification_status = 'repairing'
        repair = repair_request(diagnosis, self.state.verification, attempt)
        # A failed repair keeps the previous image and records the stage's own error.
        if target == 'implementation':
            await self._implement_service(session_id, repair)
        else:
            await self._generate_tests(session_id, repair)
        if self.state.implementation_error or self.state.tests_error:
            self.state.verification_status = 'completed'
            return False
        return True

    async def _verify_service(self, session_id: str, repair: int = 0) -> None:
        self.state.verification_status = 'running'
        try:
            self.state.verification = await workflow.execute_child_workflow(
                'VerificationWorkflow',
                args=[session_id, self.state.implementation['image_id'], self.state.tests['image_id']],
                id=self._generation_child_id('verification', session_id, repair),
                task_queue=VERIFICATION_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning('Service verification failed: %s', exc.cause or exc)
            self.state.verification_error = 'Service verification could not complete.'
            self.state.verification_status = 'error'
        else:
            self.state.verification_status = 'completed'

    def _coding_context(self) -> dict[str, Any]:
        return {
            "requirements": self.state.requirements,
            "requirements_document": self.state.requirements_document,
            "api_design": self.state.api,
        }

    def _stage_context(self, repair: dict[str, Any] | None) -> dict[str, Any]:
        context = self._coding_context()
        if self.state.checkout:
            # The authors then work inside the session's checkout of this repository.
            context['repository'] = self.state.repository
        if repair:
            context['repair'] = repair
        return context

    async def _publish(self) -> None:
        """Push the sources that passed verification to a new branch of the repository."""
        if not self.state.checkout:
            return
        if not (self.state.verification_status == 'completed' and self.state.verification
                and self.state.verification['status'] == 'passed'):
            self.state.publication_status = 'skipped'
            return
        self.state.publication_status = 'running'
        self.state.publication_error = None
        try:
            self.state.publication = await workflow.execute_activity(
                publish_repository,
                args=[workflow.info().workflow_id, self.state.repository],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Repository push failed: %s', exc.cause or exc)
            reason = str(getattr(exc.cause, 'message', '') or '')[-500:]
            self.state.publication_error = (
                'The verified sources could not be pushed to the repository.'
                + (f' {reason}' if reason else ''))
            self.state.publication_status = 'error'
        else:
            workflow.logger.info('Verified sources pushed to %s',
                                 self.state.publication['branch'])
            self.state.publication_status = 'completed'

    async def _implement_service(self, session_id: str,
                                 repair: dict[str, Any] | None = None) -> None:
        try:
            self.state.implementation = await workflow.execute_child_workflow(
                "ImplementationWorkflow",
                args=[session_id, self._stage_context(repair)],
                id=self._generation_child_id('implementation', session_id,
                                             repair['attempt'] if repair else 0),
                task_queue=IMPLEMENTATION_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning("Service implementation failed: %s", exc.cause or exc)
            self.state.implementation_error = "The service could not be implemented or built."

    async def _generate_tests(self, session_id: str,
                              repair: dict[str, Any] | None = None) -> None:
        try:
            self.state.tests = await workflow.execute_child_workflow(
                "TestGenerationWorkflow",
                args=[session_id, self._stage_context(repair)],
                id=self._generation_child_id('test-generation', session_id,
                                             repair['attempt'] if repair else 0),
                task_queue=TEST_GENERATION_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning("Service test generation failed: %s", exc.cause or exc)
            self.state.tests_error = "The service tests could not be generated or built."

    async def _expire(self) -> None:
        """After a day without messages, keep the API if it is complete."""
        workflow.logger.info("API design session idle, expiring")
        if not api_problems(self.state.api) and await self._write_document():
            return
        self.state.status = "failed"
        self.state.error = "The session expired before the API was complete."
