import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from app.config import (
        CODEGEN_TASK_QUEUE, DIAGNOSIS_TASK_QUEUE, DOCUMENTATION_TASK_QUEUE,
        IMPLEMENTATION_TASK_QUEUE, REVIEW_TASK_QUEUE, TEST_GENERATION_TASK_QUEUE,
        VERIFICATION_TASK_QUEUE,
    )
    from app.delivery.contract import with_requirements
    from app.delivery.models import (
        AgentQuestions, DeliveryProgress, DeliveryState, judge_baseline, judge_kept,
        open_review_findings, unproven_green,
    )
    from app.diagnosis.models import repair_request
    from app.github.checkout import SNAPSHOT_DIR
    from app.requirements.models import CHANGE_KINDS
    from app.review.models import (
        MAX_REVIEW_ROUNDS, ReviewFinding, ReviewRound, for_worker, review_request,
    )
    from app.temporal.activities import publish_documentation, publish_repository

# Each round is a diagnosis, a repair of one side and a new verification.
MAX_REPAIR_ROUNDS = 5
# Runs of one verification whose result is an infrastructure error, not a verdict.
VERIFICATION_ATTEMPTS = 2
# An API change may add more tests than one run reports on singly; the run still
# answers for every test of the suite.
MAX_TRACKED = 1000


@workflow.defn
class DeliveryWorkflow:
    """Turns a specified task into verified, published sources: code generation, the
    parallel service and test authors, verification with its repair loop, and the push.
    A bugfix generates no code and goes red to green instead: regression tests, proof
    that they fail against the repository's service, then the fix and the same
    verification. The repository's tests are copied aside before any of this, and every
    later comparison is with that copy. A documentation task has one author, limited to
    documentation files, and no check yet: what it wrote is pushed marked as unverified.
    A stage that fails records its own error instead of failing the run, so it can be
    retried on its own."""

    @workflow.init
    def __init__(self, state: DeliveryState) -> None:
        self.state = state
        self.spec = state.spec
        self.progress = state.progress
        # How often each child workflow ID was used in this run, to keep them unique.
        self._child_ids: dict[str, int] = {}

    @workflow.run
    async def run(self, state: DeliveryState) -> DeliveryProgress:
        if self.state.record_only:
            # A decision of the user's, such as accepting open findings, is recorded as it
            # is: nothing runs and nothing is pushed.
            return self.progress
        if not await self._snapshot():
            self.progress.verification_status = 'skipped'
        elif self.state.instructions:
            await self._instructed()
        elif self.state.retry_stage:
            await self._retry()
        else:
            if self.spec.checkout:
                self.progress.publication_status = 'waiting'
            if self.spec.kind == 'bugfix':
                await self._change()
            elif self.spec.kind == 'docs':
                await self._document()
            else:
                await self._generate()
        await self._publish()
        return self.progress

    @workflow.query
    def get_state(self) -> DeliveryState:
        return self.state

    @workflow.signal
    def agent_asked(self, asked: AgentQuestions) -> None:
        """A stage's agent paused for the user's answers, which go to its workflow."""
        self.progress.agent_questions = [
            *(item for item in self.progress.agent_questions
              if item['workflow_id'] != asked['workflow_id']), asked]

    @workflow.signal
    def agent_answered(self, workflow_id: str) -> None:
        self.progress.agent_questions = [item for item in self.progress.agent_questions
                                         if item['workflow_id'] != workflow_id]

    async def _snapshot(self) -> bool:
        """Record the repository's tests before any author runs; the activity keeps the
        first record, so a retried delivery compares with the same one."""
        # A documentation task neither reads nor writes a test.
        if not self.spec.checkout or not self.spec.language or self.spec.kind == 'docs':
            return True
        try:
            await workflow.execute_activity(
                'snapshot_repository_tests', args=[self.spec.session_id, self.spec.language],
                task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning('Repository tests not recorded: %s', exc.cause or exc)
            error = "The repository's tests could not be recorded."
            # Reported on the stage whose retry starts the delivery over.
            if self.spec.kind == 'bugfix':
                self.progress.tests_error = error
            else:
                self.progress.code_error = error
            return False
        return True

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

    async def _change(self) -> None:
        """Change the repository's service red to green: regression tests first, proof
        that they fail against the service as it is, then the fix and the whole suite."""
        self.progress.verification_status = 'waiting'
        self.progress.baseline_status = 'waiting'
        await self._generate_tests()
        # Nothing is fixed before the added tests are shown to fail without the fix.
        if not await self._reproduce():
            self.progress.verification_status = 'skipped'
            return
        # Retried tests are verified against the fix the delivery already has.
        if not self.progress.implementation:
            await self._implement_service()
        await self._verify_built()

    async def _document(self) -> None:
        """Change the repository's documentation: its author, then the check of what it
        wrote. Which check that is, and whether there is one yet, is verification's own
        matter; the result decides how the documentation is published."""
        self.progress.verification_status = 'waiting'
        try:
            self.progress.documentation = await workflow.execute_child_workflow(
                'DocumentationWorkflow',
                args=[self.spec.session_id, self._stage_context(None)],
                id=self._child_id('documentation'),
                task_queue=DOCUMENTATION_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning('Documentation failed: %s', exc.cause or exc)
            self.progress.documentation_error = 'The documentation could not be written.'
            self.progress.verification_status = 'skipped'
            return
        self.progress.verification_status = 'running'
        try:
            self.progress.verification = await workflow.execute_activity(
                'verify_documentation', self.spec.session_id,
                task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning('Documentation verification failed: %s', exc.cause or exc)
            self.progress.verification_error = 'Documentation verification could not complete.'
            self.progress.verification_status = 'error'
        else:
            self.progress.verification_status = 'completed'

    async def _reproduce(self, repair: int = 0) -> bool:
        """Run the changed suite against the service as the repository holds it: true
        when every added test fails there and every existing test passes, but for
        those a repair corrected."""
        if not self.progress.tests or self.progress.tests_error:
            self.progress.baseline_status = 'skipped'
            return False
        self.progress.baseline_status = 'running'
        self.progress.baseline = None
        self.progress.baseline_error = None
        added, changed = (self.progress.tests[key] for key in ('added_tests', 'changed_tests'))
        try:
            image = await workflow.execute_activity(
                'build_baseline', args=[self.spec.session_id, self.spec.language],
                task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            self.progress.baseline = await workflow.execute_child_workflow(
                'VerificationWorkflow',
                args=[self.spec.session_id, image['image_id'], self.progress.tests['image_id'],
                      self.spec.language, [*added, *changed]],
                id=self._child_id('baseline', repair),
                task_queue=VERIFICATION_TASK_QUEUE,
                result_type=dict,
            )
        except (ActivityError, ChildWorkflowError) as exc:
            workflow.logger.warning('Baseline run failed: %s', exc.cause or exc)
            self.progress.baseline_error = ('The tests could not be run against the service as '
                                            'the repository holds it.')
            self.progress.baseline_status = 'error'
            return False
        self.progress.baseline_status, self.progress.baseline_error = judge_baseline(
            self.progress.baseline, added, changed)
        workflow.logger.info('Baseline run: %s', self.progress.baseline_status)
        return self.progress.baseline_status == 'reproduced'

    def _child_id(self, stage: str, repair: int = 0) -> str:
        child_id = f'{stage}-{self.spec.session_id}'
        if self.state.retry_stage or self.state.instructions:
            child_id += f'-{workflow.info().run_id}'
        if repair:
            child_id += f'-repair-{repair}'
        count = self._child_ids.get(child_id, 0) + 1
        self._child_ids[child_id] = count
        # Review rounds verify again, with repair rounds of their own.
        return child_id if count == 1 else f'{child_id}-{count}'

    async def _retry(self) -> None:
        if self.state.retry_stage == 'repository':
            # Generation and verification already succeeded; only the push is repeated.
            return
        if self.state.retry_stage == 'review':
            if self.progress.correction_failed:
                await self._retry_correction()
            else:
                # The verified delivery is reviewed again; a new run of review rounds.
                await self._review_rounds()
            return
        if self.spec.kind == 'docs':
            await self._document()
            return
        if self.spec.kind == 'bugfix':
            if self.state.retry_stage == 'tests':
                # New tests have to be shown to fail without the fix again.
                await self._change()
                return
        elif self.state.retry_stage == 'server' and not self.progress.code:
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
            if self.spec.kind == 'api_change':
                await self._prove_kept()
            if self._passed():
                await self._review_rounds()
        else:
            self.progress.verification_status = 'skipped'

    def _passed(self) -> bool:
        """Whether verification showed the implementation and the tests passing against
        each other, with everything a change needs proved."""
        return bool(self.progress.verification_status == 'completed'
                    and self.progress.verification
                    and self.progress.verification['status'] == 'passed'
                    and not self.progress.verification_error
                    and not self.progress.implementation_error
                    and not self.progress.tests_error
                    and (self.spec.kind != 'bugfix'
                         or self.progress.baseline_status == 'reproduced'))

    async def _review_rounds(self, instructions: str | None = None) -> None:
        """Review the verified delivery's changes; while findings remain, send them to the
        worker they concern, verify again and review again, at most MAX_REVIEW_ROUNDS
        times. Findings left after the last round are open: the delivery is pushed as
        unverified and the user decides about them."""
        for count in range(1, MAX_REVIEW_ROUNDS + 1):
            findings = await self._review(instructions)
            # Only the first round reviews fixes made from the user's instructions.
            instructions = None
            if findings is None:
                return
            if not findings:
                self.progress.verification_status = 'completed'
                return
            if count == MAX_REVIEW_ROUNDS:
                break
            if not await self._correct(findings, self.progress.reviews[-1]['round']):
                return
        workflow.logger.info('Review findings remain after %d rounds', MAX_REVIEW_ROUNDS)
        self.progress.verification_status = 'findings_open'

    async def _review(self, instructions: str | None) -> list[ReviewFinding] | None:
        """One review round; None when the review could not run."""
        self.progress.verification_status = 'reviewing'
        number = len(self.progress.reviews) + 1
        context = self._coding_context()
        if instructions:
            context['instructions'] = instructions
        try:
            result = await workflow.execute_child_workflow(
                'ReviewWorkflow', args=[self.spec.session_id, context],
                id=self._child_id('review', number),
                task_queue=REVIEW_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning('Code review failed: %s', exc.cause or exc)
            self.progress.review_error = 'The code review could not run.'
            self.progress.verification_status = 'error'
            return None
        self.progress.review_error = None
        findings: list[ReviewFinding] = list(result.get('findings') or [])
        round_: ReviewRound = {'round': number, 'findings': findings}
        if instructions:
            round_['instructions'] = instructions
        self.progress.reviews.append(round_)
        workflow.logger.info('Review round %d: %d findings', number, len(findings))
        return findings

    async def _correct(self, findings: list[ReviewFinding], number: int,
                       instructions: str | None = None) -> bool:
        """Have each worker the findings concern correct them, or with the user's
        instructions both, then verify again. True when verification passed again.
        A correction of the review's own findings that did not pass is left for the user
        to retry; one from instructions leaves the open findings to decide about again."""
        self.progress.verification_status = 'repairing'
        workers = [worker for worker in ('implementation', 'tests')
                   if instructions or for_worker(findings, worker)]
        await asyncio.gather(*(
            (self._implement_service if worker == 'implementation' else self._generate_tests)(
                review_request(findings, worker, number, instructions))
            for worker in workers))
        if await self._verify_correction(workers, number):
            return True
        self.progress.correction_failed = not instructions
        return False

    async def _retry_correction(self) -> None:
        """Go on from a correction of review findings that did not pass, then with the
        review rounds. A worker that failed, or corrected tests of a bugfix that no longer
        failed without the fix, correct the findings again; otherwise the corrected
        delivery is only verified again."""
        self.progress.correction_failed = False
        number = self.progress.reviews[-1]['round']
        if (self.progress.implementation_error or self.progress.tests_error
                or self.spec.kind == 'bugfix' and self.progress.baseline_status != 'reproduced'):
            self.progress.implementation_error = None
            self.progress.tests_error = None
            corrected = await self._correct(open_review_findings(self.progress), number)
        else:
            self.progress.verification_status = 'repairing'
            corrected = await self._verify_correction([], number)
            self.progress.correction_failed = not corrected
        if corrected:
            await self._review_rounds()

    async def _verify_correction(self, workers: list[str], number: int) -> bool:
        """Verify the delivery as the workers corrected it; true when it passed again."""
        if self.progress.implementation_error or self.progress.tests_error:
            self.progress.verification_status = 'error'
            return False
        if ('tests' in workers and self.spec.kind == 'bugfix'
                and not await self._reproduce(number)):
            # Corrected added tests that no longer fail without the fix prove nothing.
            self.progress.verification_status = 'error'
            return False
        self.progress.verification = None
        self.progress.verification_error = None
        await self._verify_and_repair()
        if self.spec.kind == 'api_change':
            await self._prove_kept()
        if self._passed():
            return True
        if self.progress.verification_status == 'completed':
            # The tests no longer pass: the delivery is not pushed this way.
            self.progress.verification_status = 'error'
            self.progress.verification_error = (
                self.progress.verification_error
                or 'Verification failed after correcting the review findings.')
        return False

    async def _instructed(self) -> None:
        """The user's instructions about the open review findings: a round of fixes from
        them, verification, and a new run of review rounds. If verification fails for
        good the earlier unverified push stays and the flag stays raised."""
        findings = open_review_findings(self.progress)
        number = len(self.progress.reviews) + 1
        instructions = self.state.instructions
        if await self._correct(findings, number, instructions):
            await self._review_rounds(instructions)

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

    async def _prove_kept(self) -> None:
        """An API change may rewrite the repository's tests, but only those its service
        no longer passes: run each one it changed or removed as the repository held it."""
        tests = self.progress.tests
        touched = [*tests['changed_tests'], *tests['removed_tests']]
        if (not touched or self.progress.verification_error
                or self.progress.verification_status != 'completed'
                or self.progress.verification['status'] != 'passed'):
            return
        self.progress.verification_status = 'running'
        try:
            image = await workflow.execute_activity(
                'build_repository_tests', args=[self.spec.session_id, self.spec.language],
                task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            self.progress.existing_tests = await workflow.execute_child_workflow(
                'VerificationWorkflow',
                args=[self.spec.session_id, self.progress.implementation['image_id'],
                      image['image_id'], self.spec.language, touched, SNAPSHOT_DIR],
                id=self._child_id('existing-tests'),
                task_queue=VERIFICATION_TASK_QUEUE,
                result_type=dict,
            )
        except (ActivityError, ChildWorkflowError) as exc:
            workflow.logger.warning('Run of the repository tests failed: %s', exc.cause or exc)
            self.progress.verification_error = (
                "The repository's tests could not be run against the changed service.")
        else:
            self.progress.verification_error = judge_kept(self.progress.existing_tests, touched)
        self.progress.verification_status = 'completed'

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
            {'attempt': attempt, 'target': target, 'tests': diagnosis['tests'],
             'summary': diagnosis['summary']})
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
        if (target == 'tests' and self.spec.kind == 'bugfix'
                and not await self._reproduce(attempt)):
            # Repaired added tests that no longer fail without the fix prove nothing.
            self.progress.verification_status = 'completed'
            return False
        return True

    async def _verify_service(self, repair: int = 0) -> None:
        self.progress.verification_status = 'running'
        change = self.spec.kind in CHANGE_KINDS
        # A run that could not complete says nothing about the code, so it runs once more.
        for attempt in range(1, VERIFICATION_ATTEMPTS + 1):
            try:
                self.progress.verification = await workflow.execute_child_workflow(
                    'VerificationWorkflow',
                    args=[self.spec.session_id, self.progress.implementation['image_id'],
                          self.progress.tests['image_id'], self.spec.language,
                          # The run of a change answers for every test of its suite.
                          self.progress.tests['added_tests'][:MAX_TRACKED] if change else None],
                    id=self._child_id('verification', repair),
                    task_queue=VERIFICATION_TASK_QUEUE,
                    result_type=dict,
                )
            except ChildWorkflowError as exc:
                workflow.logger.warning('Service verification failed: %s', exc.cause or exc)
                self.progress.verification_error = 'Service verification could not complete.'
                self.progress.verification_status = 'error'
                return
            if self.progress.verification['status'] != 'error':
                break
            workflow.logger.warning('Verification run %d could not complete: %s', attempt,
                                    self.progress.verification['error'])
        self.progress.verification_status = 'completed'
        if change and self.progress.verification['status'] == 'passed':
            # Red without the fix is only half of it: every test must be seen green.
            self.progress.verification_error = unproven_green(self.progress.verification)

    def _coding_context(self) -> dict[str, Any]:
        context = {
            "requirements": self.spec.requirements,
            "requirements_document": self.spec.requirements_document,
            "api_design": self.spec.api,
            "language": self.spec.language,
        }
        if self.spec.brief:
            # A change to an existing service: the contract above is the repository's.
            context['brief'] = self.spec.brief
        return context

    def _stage_context(self, repair: dict[str, Any] | None) -> dict[str, Any]:
        context = self._coding_context()
        if self.spec.checkout:
            # The authors then work inside the session's checkout of this repository.
            context['repository'] = self.spec.repository
        if repair:
            context['repair'] = repair
        return context

    async def _publish(self) -> None:
        """Push the sources that passed verification, with their contract, to a new branch
        of the repository. Documentation that no check exists for yet is pushed too, and
        marked as unverified."""
        if not self.spec.checkout:
            return
        # Tests that pass with review findings left after the last round are pushed too,
        # marked as unverified, with the findings in the commit message.
        open_findings = self.progress.verification_status == 'findings_open'
        result = (self.progress.verification
                  if self.progress.verification_status == 'completed' or open_findings
                  else None)
        documentation = self.spec.kind == 'docs'
        # Only a documentation task may go on without a check, and only when
        # verification says it has none: a check that ran and did not pass stops it too.
        unverified = bool(documentation and result and result['status'] == 'not_implemented')
        skip = not (unverified or result and result['status'] == 'passed')
        if not skip and self.spec.kind in CHANGE_KINDS and (
                self.progress.verification_error
                or self.spec.kind == 'bugfix' and self.progress.baseline_status != 'reproduced'):
            # Green alone is not enough for a change: all of its tests must have run, a
            # bugfix's must have been red without the fix, and an API change must have
            # kept the repository's tests its service still passes.
            skip = True
        if skip:
            if self.progress.open_findings and self.progress.publication:
                # A run started by the user's instructions that did not pass leaves the
                # earlier unverified push on the branch, and the flag raised.
                self.progress.publication_status = 'completed'
            else:
                self.progress.publication_status = 'skipped'
            return
        self.progress.publication_status = 'running'
        self.progress.publication_error = None
        findings = open_review_findings(self.progress) if open_findings else []
        if documentation:
            # The documentation alone: the service, its tests and its contract stay.
            push, args = publish_documentation, [self.spec.language, not unverified]
        else:
            contract = {
                'language': self.spec.language,
                'requirements': self.spec.requirements,
                'requirements_document': self.spec.requirements_document,
                'api': self.spec.api,
            }
            if self.spec.brief:
                # The repository's contract grows by what the change required of the service.
                contract = with_requirements(contract, self.spec.brief['requirements'],
                                             self.spec.brief['retired'])
            push, args = publish_repository, [contract, self.spec.kind, findings]
        try:
            self.progress.publication = await workflow.execute_activity(
                push,
                args=[self.spec.session_id, self.spec.repository, *args],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Repository push failed: %s', exc.cause or exc)
            reason = str(getattr(exc.cause, 'message', '') or '')[-500:]
            self.progress.publication_error = (
                f'The {"documentation" if documentation else "verified sources"} could not be '
                'pushed to the repository.' + (f' {reason}' if reason else ''))
            self.progress.publication_status = 'error'
        else:
            workflow.logger.info('Sources pushed to %s', self.progress.publication['branch'])
            self.progress.publication_status = 'completed'
            if not documentation:
                # Raised for the user to accept the delivery or give instructions.
                self.progress.open_findings = open_findings
                if open_findings:
                    self.progress.findings_acceptance = None

    @staticmethod
    def _stage_name(stage: str, repair: dict[str, Any] | None) -> str:
        """Corrections of review findings are told apart from repairs of failed tests."""
        return f'{stage}-review' if repair and 'review' in repair else stage

    async def _implement_service(self, repair: dict[str, Any] | None = None) -> None:
        try:
            self.progress.implementation = await workflow.execute_child_workflow(
                "ImplementationWorkflow",
                args=[self.spec.session_id, self._stage_context(repair)],
                id=self._child_id(self._stage_name('implementation', repair),
                                  repair['attempt'] if repair else 0),
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
                id=self._child_id(self._stage_name('test-generation', repair),
                                  repair['attempt'] if repair else 0),
                task_queue=TEST_GENERATION_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning("Service test generation failed: %s", exc.cause or exc)
            self.progress.tests_error = "The service tests could not be generated or built."
