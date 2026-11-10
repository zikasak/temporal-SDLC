from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError

from app.delivery.flow.stages import (
    COMPONENT_REPAIRS_PATCH, MAX_TRACKED, RESTORE_KEPT_PATCH, VERIFICATION_ATTEMPTS,
    restore_finding,
)

with workflow.unsafe.imports_passed_through():
    from app.config import DIAGNOSIS_TASK_QUEUE, VERIFICATION_TASK_QUEUE
    from app.delivery.models import judge_kept, kept_tests, restorable_tests, unproven_green
    from app.diagnosis.models import repair_request
    from app.github.checkout import SNAPSHOT_DIR
    from app.requirements.models import CHANGE_KINDS
    from app.review.models import review_request

# Each round is a diagnosis, a repair of one side and a new verification.
MAX_REPAIR_ROUNDS = 5
# Rounds in which the test author of an API change puts back the repository's tests the
# changed service still passes, each verified again.
MAX_RESTORE_ROUNDS = 2
# Failed tests a verification that is not repaired names.
NAMED_FAILURES = 5
# What an onboarding asks the user when the diagnosis finds the existing code at fault.
BUG_QUESTION = ('A contract test fails against the existing code. Does the failure reveal a '
                'bug in the existing code?')
BUG_CONFIRMED = 'Yes: it is a bug, change the existing code'
SETUP_AT_FAULT = 'No: the Dockerfile or the service setup is at fault, keep the code'
TESTS_AT_FAULT = 'No: the code is right, repair the tests and the contract'
BUG_SUMMARY_CHARS = 600


class Verification:
    """Verification of the implementation and the tests against each other, with
    its repair rounds: diagnosis, a repair of the side at fault, and a new run."""

    async def _verify_built(self) -> None:
        """Verification needs both images; a failed build leaves nothing to verify."""
        if self.progress.component_tests:
            workflow.deprecate_patch(COMPONENT_REPAIRS_PATCH)
            await self._verify_components_and_repair()
            if self._components_passed():
                await self._review_rounds()
            return
        if (self.progress.implementation and self.progress.tests
                and not self.progress.implementation_error and not self.progress.tests_error):
            await self._verify_and_repair()
            if self.spec.kind == 'api_change':
                await self._prove_kept()
                workflow.deprecate_patch(RESTORE_KEPT_PATCH)
                await self._restore_kept()
            if self._passed():
                await self._review_rounds()
        else:
            self.progress.verification_status = 'skipped'

    def _passed(self) -> bool:
        """Whether verification showed the implementation and the tests passing against
        each other, with everything a change needs proved."""
        if self.progress.component_tests:
            return self._components_passed()
        return bool(self.progress.verification_status == 'completed'
                    and self.progress.verification
                    and self.progress.verification['status'] == 'passed'
                    and not self.progress.verification_error
                    and not self.progress.implementation_error
                    and not self.progress.tests_error
                    and (self.spec.kind != 'bugfix'
                         or self.progress.baseline_status == 'reproduced'))

    def _components_passed(self, findings_open: bool = False) -> bool:
        """Whether verification showed a change with component tests passing both suites,
        after both were red without it; with `findings_open`, also when code review
        findings remained after it."""
        contract, component = self.progress.verification, self.progress.component_verification
        statuses = ('completed', 'findings_open') if findings_open else ('completed',)
        return bool(self.progress.verification_status in statuses
                    and (contract is None or contract['status'] == 'passed')
                    and component and component['status'] == 'passed'
                    and not self.progress.verification_error
                    and not self.progress.implementation_error
                    and not self.progress.tests_error
                    and not self.progress.component_tests_error
                    and self.progress.baseline_status == 'reproduced')

    def _component_tests_failed(self) -> bool:
        """Whether verification of a change with component tests ran, and tests of either
        suite failed against the changed code."""
        return bool(self.progress.verification_status == 'completed'
                    and not self.progress.implementation_error
                    and any(run and run['status'] == 'failed' for run in (
                        self.progress.verification, self.progress.component_verification)))

    async def _verify_components_and_repair(self) -> None:
        """Verify a change with component tests; while tests of either suite fail, repair
        the side the diagnosis condemns, and only the suite it names of the tests, then
        verify again."""
        await self._verify_components()
        if not self._component_tests_failed():
            return
        for attempt in range(1, MAX_REPAIR_ROUNDS + 1):
            if not await self._repair(attempt):
                return
            await self._verify_components(attempt)
            if not self._component_tests_failed():
                return
        self.progress.verification_error = (
            f'Tests still fail after {MAX_REPAIR_ROUNDS} automatic repair rounds.')

    async def _verify_components(self, repair: int = 0) -> None:
        """Verify a change with component tests: the changed code must still conform to its
        component contract, and then pass its contract tests, if it has any, and its
        component tests, built with it."""
        if (not self.progress.implementation or self.progress.implementation_error
                or self.progress.tests_error or self.progress.component_tests_error):
            self.progress.verification_status = 'skipped'
            return
        self.progress.verification_status = 'running'
        self.progress.verification = None
        self.progress.verification_error = None
        self.progress.component_verification = None
        service_image = self.progress.implementation['image_id']
        try:
            # The fix is held to the component contract before anything is run against it.
            runner = await workflow.execute_activity(
                'build_component_runner', args=[self.spec.session_id, self.spec.language],
                task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            if 'conformance_error' in runner:
                # The fix is at fault, whatever the tests: retrying the server writes it
                # again, while retrying the tests would keep it.
                self.progress.implementation_error = (
                    f'{runner["conformance_error"]} The fix has to keep every signature of '
                    'the component contract, so it was not verified. Retry the '
                    'implementation to write the fix again.')
                self.progress.verification_status = 'skipped'
                return
            suite = await self._contract_suite()
            if suite:
                test_image, suite_dir, added, _ = suite
                for attempt in range(1, VERIFICATION_ATTEMPTS + 1):
                    self.progress.verification = await self._child(
                        'VerificationWorkflow', VERIFICATION_TASK_QUEUE,
                        self._child_id('verification', repair), service_image, test_image,
                        self.spec.language, added[:MAX_TRACKED], suite_dir)
                    if self.progress.verification['status'] != 'error':
                        break
            # A passed run is judged as a whole, so it tracks what one run can report on.
            self.progress.component_verification = await self._run_components(
                runner['image_id'], self.progress.component_tests['added_tests'][:MAX_TRACKED])
        except (ActivityError, ChildWorkflowError) as exc:
            workflow.logger.warning('Verification of the change failed: %s', exc.cause or exc)
            self.progress.verification_error = 'Service verification could not complete.'
            self.progress.verification_status = 'error'
            return
        self.progress.verification_status = 'completed'
        self.progress.verification_error = self._judge_components()
        workflow.logger.info('Verification of the change: %s',
                             'rejected' if self.progress.verification_error else 'passed')

    def _judge_components(self) -> str | None:
        """Why verification of a change with component tests did not show it working;
        None when it did."""
        contract, component = self.progress.verification, self.progress.component_verification
        failed = []
        for name, run in (('contract tests', contract), ('component tests', component)):
            if run is None:
                continue
            if run['status'] == 'error':
                return (f'The {name} could not be run against the changed code: '
                        f'{run.get("error") or "the run did not complete"}.')
            if run['status'] == 'failed':
                named = ', '.join(f'{item["class_name"]}#{item["name"]}'
                                  for item in run['test_failures'][:NAMED_FAILURES])
                failed.append(f'{name} {run["failures"] + run["errors"]} of {run["tests"]}'
                              + (f' ({named})' if named else ''))
        if failed:
            workflow.deprecate_patch(COMPONENT_REPAIRS_PATCH)
            return f'Tests fail against the changed code: {"; ".join(failed)}.'
        return unproven_green(contract, component)

    def _tests_failed(self) -> bool:
        return bool(self.progress.verification_status == 'completed'
                    and self.progress.verification
                    and self.progress.verification['status'] == 'failed')

    async def _verify_and_repair(self) -> None:
        """Verify; while tests fail, repair the side at fault and verify again."""
        if self.progress.component_tests:
            await self._verify_components_and_repair()
            return
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
            self.progress.existing_tests = await self._child(
                'VerificationWorkflow', VERIFICATION_TASK_QUEUE,
                self._child_id('existing-tests'), self.progress.implementation['image_id'],
                image['image_id'], self.spec.language, touched, SNAPSHOT_DIR)
        except (ActivityError, ChildWorkflowError) as exc:
            workflow.logger.warning('Run of the repository tests failed: %s', exc.cause or exc)
            self.progress.verification_error = (
                "The repository's tests could not be run against the changed service.")
        else:
            self.progress.verification_error = judge_kept(self.progress.existing_tests, touched)
            # Once seen passing as the repository holds them, they stay so for every
            # later author: a repair that rewrote one again would only be rejected again.
            self.state.kept_tests = sorted({
                *self.state.kept_tests, *kept_tests(self.progress.existing_tests, touched)})
        self.progress.verification_status = 'completed'

    async def _restore_kept(self) -> None:
        """While verification of an API change passed but rejected its suite for tests of
        the repository it changed or removed that the changed service still passes, have
        the test author put them back as the repository holds them and verify again, at
        most MAX_RESTORE_ROUNDS times. A rejection left stays the verification's error."""
        for attempt in range(1, MAX_RESTORE_ROUNDS + 1):
            restore = restorable_tests(self.progress)
            if not restore or not self.progress.verification_error:
                return
            workflow.logger.info('Restore round %d: %d repository tests', attempt, len(restore))
            self.progress.verification_status = 'repairing'
            await self._generate_tests(
                review_request([restore_finding(restore)], 'tests', attempt, restore=restore))
            if self.progress.tests_error:
                # The rejected suite's image stays; the author's failure is the way on.
                self.progress.verification_status = 'completed'
                return
            self.progress.verification = None
            self.progress.verification_error = None
            self.progress.existing_tests = None
            await self._verify_and_repair()
            await self._prove_kept()

    async def _verify_service(self, repair: int = 0) -> None:
        self.progress.verification_status = 'running'
        change = self.spec.kind in CHANGE_KINDS
        # A run that could not complete says nothing about the code, so it runs once more.
        for attempt in range(1, VERIFICATION_ATTEMPTS + 1):
            try:
                self.progress.verification = await self._child(
                    'VerificationWorkflow', VERIFICATION_TASK_QUEUE,
                    self._child_id('verification', repair),
                    self.progress.implementation['image_id'], self.progress.tests['image_id'],
                    self.spec.language,
                    # The run of a change answers for every test of its suite.
                    self.progress.tests['added_tests'][:MAX_TRACKED] if change else None)
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

    async def _repair(self, attempt: int) -> bool:
        """Decide which side the failed tests condemn and have its own stage correct it."""
        self.progress.verification_status = 'diagnosing'
        context = {**self._coding_context(), 'verification': self.progress.verification}
        if self.progress.component_tests:
            # Both suites' runs; the contract tests are the repository's own when the change
            # gave them no criterion.
            context['component_verification'] = self.progress.component_verification
            context['contract_suite'] = 'tests' if self.progress.tests else SNAPSHOT_DIR
        try:
            diagnosis = await self._child(
                'DiagnosisWorkflow', DIAGNOSIS_TASK_QUEUE,
                self._child_id('diagnosis', attempt), context)
        except ChildWorkflowError as exc:
            workflow.logger.warning('Verification diagnosis failed: %s', exc.cause or exc)
            self.progress.verification_error = 'The failed tests could not be diagnosed.'
            self.progress.verification_status = 'completed'
            return False
        target = diagnosis['target']
        code_change = False
        if self.spec.onboarding and target == 'implementation':
            # The existing code changes only when the user confirms a bug in it.
            diagnosis, code_change = await self._confirm_bug(diagnosis)
            target = diagnosis['target']
        workflow.logger.info('Repair round %d: correcting the %s', attempt, target)
        suite = diagnosis.get('suite')
        self.progress.repairs.append(
            {'attempt': attempt, 'target': target, 'tests': diagnosis['tests'],
             'summary': diagnosis['summary'], **({'suite': suite} if suite else {})})
        self.progress.verification_status = 'repairing'
        repair = repair_request(diagnosis, self.progress.verification, attempt,
                                self.progress.component_verification)
        if code_change:
            repair['code_change'] = True
        # A failed repair keeps the previous image and records the stage's own error.
        if target == 'implementation':
            await self._implement_service(repair)
        else:
            await self._generate_tests(repair, suite)
        if (self.progress.implementation_error or self.progress.tests_error
                or self.progress.component_tests_error):
            self.progress.verification_status = 'completed'
            return False
        if (target == 'tests' and self.spec.kind == 'bugfix'
                and not await self._reproduce(attempt)):
            # Repaired added tests that no longer fail without the fix prove nothing.
            self.progress.verification_status = 'completed'
            return False
        return True

    async def _confirm_bug(self, diagnosis: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Ask the user whether the failure the diagnosis traced to the existing code is a
        bug in it, and wait for the answer without a timeout. Returns the diagnosis as the
        answer settles it, and whether the existing code may change."""
        workflow_id = workflow.info().workflow_id
        summary = ' '.join(str(diagnosis.get('summary') or '').split())
        if len(summary) > BUG_SUMMARY_CHARS:
            summary = summary[:BUG_SUMMARY_CHARS - 1] + '…'
        question = BUG_QUESTION + (f' The diagnosis: {summary}' if summary else '')
        self._bug_answer = None
        self._bug_question = True
        self.progress.agent_questions = [
            *(item for item in self.progress.agent_questions
              if item['workflow_id'] != workflow_id),
            {'workflow_id': workflow_id, 'stage': 'diagnosis',
             'questions': [{'question': question,
                            'options': [BUG_CONFIRMED, SETUP_AT_FAULT, TESTS_AT_FAULT]}]}]
        await workflow.wait_condition(lambda: self._bug_answer is not None)
        self._bug_question = False
        self.progress.agent_questions = [item for item in self.progress.agent_questions
                                         if item['workflow_id'] != workflow_id]
        answer = self._bug_answer or ''
        workflow.logger.info('The user answered whether the failure is a bug: %s', answer[:80])
        own = '' if answer in (BUG_CONFIRMED, SETUP_AT_FAULT, TESTS_AT_FAULT) else (
            f' The user answered: {answer}')
        if answer == BUG_CONFIRMED or (own and answer.lower().startswith('yes')):
            return {**diagnosis, 'instructions': (
                f'{diagnosis["instructions"]}\n\nThe user confirmed that this is a bug in '
                f'the existing code: fix it there.{own}')}, True
        if answer == SETUP_AT_FAULT:
            return {**diagnosis, 'instructions': (
                f'{diagnosis["instructions"]}\n\nThe user said the existing code is right: '
                f'correct the Dockerfile or the service setup only.{own}')}, False
        failing = [{'class_name': item['class_name'], 'name': item['name']}
                   for item in (self.progress.verification or {}).get('test_failures') or []]
        return {**diagnosis, 'target': 'tests', 'tests': failing, 'instructions': (
            f'The user confirmed that the existing service behaves correctly here, so the '
            f'failing tests are wrong: correct them to expect what the service does, as far '
            f'as the contract allows. What the diagnosis found: '
            f'{diagnosis["summary"]}{own}')}, False

    @workflow.update
    def answer_questions(self, answers: list[str]) -> None:
        """The user's answer to the onboarding's question whether a failing contract test
        reveals a bug in the existing code."""
        self._bug_answer = answers[0].strip()

    @answer_questions.validator
    def validate_answer_questions(self, answers: list[str]) -> None:
        if not self._bug_question or self._bug_answer is not None:
            raise ValueError('These questions are no longer open.')
        if len(answers) != 1 or not answers[0].strip():
            raise ValueError('Answer the question with one answer.')
