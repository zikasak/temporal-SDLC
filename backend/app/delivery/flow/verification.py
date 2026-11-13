from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

from app.agent_workflows.workflows import PROVIDER_WAIT_TOTAL, provider_wait

from app.delivery.flow.stages import (
    COMPONENT_REPAIRS_PATCH, MAX_TRACKED, RESTORE_KEPT_PATCH, VERIFICATION_ATTEMPTS,
    restore_finding,
)

with workflow.unsafe.imports_passed_through():
    from app.delivery.causes import with_cause
    from app.delivery.rounds import decide, outcome
    from app.llm.limits import PROVIDER_LIMIT_ERROR
    from app.verification.checks import fallback_analysis
    from app.config import DIAGNOSIS_TASK_QUEUE, VERIFICATION_TASK_QUEUE
    from app.delivery.models import judge_kept, kept_tests, restorable_tests, unproven_green
    from app.diagnosis.models import repair_request
    from app.github.checkout import SNAPSHOT_DIR
    from app.requirements.models import CHANGE_KINDS
    from app.review.models import review_request

# Each round is a diagnosis, a repair of one side and a new verification. Within the
# ceiling each round has to be earned by progress (app.delivery.rounds); runs that began
# before that keep the old, fixed number of rounds.
MAX_REPAIR_ROUNDS = 12
LEGACY_REPAIR_ROUNDS = 5
# Marks the workflow code that runs the repository's checks script once the tests pass,
# repairs what it reports, and decides each further repair round from progress.
REPOSITORY_CHECKS_PATCH = 'repository-checks'
CHECKS_UNREPAIRED = "The repository's checks still fail"
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
                    and not self._checks_failed()
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
                    and not self._checks_failed()
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
        if workflow.patched(REPOSITORY_CHECKS_PATCH):
            await self._judged_rounds(
                self._verify_components, self._component_tests_failed,
                lambda: self._components_passed(findings_open=True)
                or self._components_green())
            return
        await self._verify_components()
        if not self._component_tests_failed():
            return
        for attempt in range(1, LEGACY_REPAIR_ROUNDS + 1):
            if not await self._repair(attempt):
                return
            await self._verify_components(attempt)
            if not self._component_tests_failed():
                return
        self.progress.verification_error = (
            f'Tests still fail after {LEGACY_REPAIR_ROUNDS} automatic repair rounds.')

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
            self.progress.verification_error = with_cause(
                    'Service verification could not complete.', exc)
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
        if workflow.patched(REPOSITORY_CHECKS_PATCH):
            await self._judged_rounds(self._verify_service, self._tests_failed,
                                      self._service_green)
            return
        await self._verify_service()
        if not self._tests_failed():
            return
        for attempt in range(1, LEGACY_REPAIR_ROUNDS + 1):
            if not await self._repair(attempt):
                return
            await self._verify_service(attempt)
            if not self._tests_failed():
                return
        self.progress.verification_error = (
            f'Tests still fail after {LEGACY_REPAIR_ROUNDS} automatic repair rounds.')

    def _service_green(self) -> bool:
        """Whether the contract tests passed against the service: the repository's checks
        run then."""
        verification = self.progress.verification
        return bool(self.progress.verification_status == 'completed' and verification
                    and verification['status'] == 'passed'
                    and not self.progress.implementation_error
                    and not self.progress.tests_error)

    def _components_green(self) -> bool:
        contract, component = self.progress.verification, self.progress.component_verification
        return bool(self.progress.verification_status == 'completed'
                    and (contract is None or contract['status'] == 'passed')
                    and component and component['status'] == 'passed'
                    and not self.progress.implementation_error)

    async def _judged_rounds(self, verify: Callable[..., Awaitable[None]],
                             tests_failed: Callable[[], bool],
                             green: Callable[[], bool]) -> None:
        """Verify; once the tests pass, run the repository's checks. While either fails,
        repair what failed and verify again, as long as each round shows progress
        (app.delivery.rounds), up to MAX_REPAIR_ROUNDS."""
        self.progress.repair_decision = None
        self.progress.checks = None
        history: list[dict[str, Any]] = []
        attempt = 0
        await verify()
        while True:
            failing = None
            if tests_failed():
                failing = 'tests'
            elif green():
                await self._run_checks()
                if self._checks_failed():
                    failing = 'checks'
            if failing is None:
                return
            history.append(self._round_outcome(failing))
            decision = decide(history, MAX_REPAIR_ROUNDS)
            self.progress.repair_decision = {**decision, 'rounds': attempt}
            if not decision['go']:
                workflow.logger.info('No further repair round: %s', decision['reason'])
                what = (f'{CHECKS_UNREPAIRED} ({self._checks_summary().rstrip(".")})'
                        if failing == 'checks' else 'Tests still fail')
                self.progress.verification_error = (
                    f'{what} after {attempt} automatic repair rounds. {decision["reason"]}'
                    if attempt else f'{what}. {decision["reason"]}')
                return
            attempt += 1
            repaired = await (self._repair(attempt) if failing == 'tests'
                              else self._repair_checks(attempt))
            if not repaired:
                return
            await verify(attempt)

    def _round_outcome(self, failing: str) -> dict[str, Any]:
        """What is failing now, for the decision whether to go on repairing."""
        if failing == 'checks':
            signature = (self.progress.checks or {}).get('signature') or ['checks failed']
            return outcome(len(signature), [f'checks: {item}' for item in signature])
        runs = [run for run in (self.progress.verification,
                                self.progress.component_verification) if run]
        failures = sum(run['failures'] + run['errors'] for run in runs)
        return outcome(failures, [f'test: {item["class_name"]}#{item["name"]}'
                                  for run in runs for item in run.get('test_failures') or []])

    def _checks_failed(self) -> bool:
        checks = self.progress.checks
        return bool(checks and checks.get('status') == 'failed')

    def _checks_summary(self) -> str:
        checks = self.progress.checks or {}
        analysis = checks.get('analysis') or {}
        return (analysis.get('summary') or next(iter(checks.get('signature') or []), '')
                or f'the checks script exited with status {checks.get("exit_code")}')

    async def _run_checks(self) -> None:
        """Run the repository's checks script over the repository as it would be pushed;
        when it fails, have the LLM say where the problem is, from the excerpt of its
        output. A run that could not complete, or that failed for the sandbox rather than
        the code, is recorded and does not stop the delivery: it says nothing about the
        code."""
        if not self.spec.checkout or not self.spec.language:
            return
        self.progress.verification_status = 'checking'
        try:
            checks = await workflow.execute_activity(
                'run_repository_checks', args=[self.spec.session_id, self.spec.language],
                task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
                start_to_close_timeout=timedelta(minutes=55),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning("The repository's checks did not run: %s", exc.cause or exc)
            checks = {'status': 'error', 'exit_code': None, 'excerpt': '', 'signature': [],
                      'log': None,
                      'error': with_cause("The repository's checks could not be run.", exc)}
        if checks['status'] == 'failed':
            checks['analysis'] = await self._analyze_checks(checks['excerpt'])
            if checks['analysis'].get('cause') == 'environment':
                # The sandbox, not the code, failed them: no author could repair that, so
                # it counts as a run that could not complete.
                workflow.logger.warning("The repository's checks failed for the sandbox: %s",
                                        checks['analysis'].get('summary'))
                checks['status'] = 'error'
                checks['error'] = ("The repository's checks could not run in the sandbox: "
                                   f"{checks['analysis'].get('summary') or 'see their log'}")
        self.progress.checks = checks
        self.progress.verification_status = 'completed'
        workflow.logger.info("The repository's checks: %s", checks['status'])

    async def _analyze_checks(self, excerpt: str) -> dict[str, Any]:
        """The LLM's analysis of a failure of the repository's checks. While the
        provider's limits are spent it waits, as the coding agents do; any other failure
        gives the analysis made without the LLM."""
        waited, attempt = timedelta(), 0
        while True:
            try:
                return await workflow.execute_activity(
                    'analyze_checks_failure', args=[self.spec.session_id, excerpt],
                    task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
            except ActivityError as exc:
                cause = exc.cause
                if not (isinstance(cause, ApplicationError)
                        and cause.type == PROVIDER_LIMIT_ERROR):
                    workflow.logger.warning('The checks failure was not analysed: %s',
                                            cause or exc)
                    return fallback_analysis(excerpt)
                details = cause.details[0] if cause.details else {}
                attempt += 1
                wait = provider_wait(attempt, (details or {}).get('retry_after'))
                if waited + wait > PROVIDER_WAIT_TOTAL:
                    return fallback_analysis(excerpt)
                workflow.logger.warning('The checks analysis waits %s for its provider', wait)
                await workflow.sleep(wait)
                waited += wait

    async def _repair_checks(self, attempt: int) -> bool:
        """Have the implementation's author fix what the repository's checks reported,
        from the analysis and the excerpt of their output."""
        checks = self.progress.checks or {}
        analysis = checks.get('analysis') or fallback_analysis(checks.get('excerpt') or '')
        files = analysis.get('files') or []
        instructions = analysis['instructions'] + (
            f'\nFiles to look at first: {", ".join(files)}.' if files else '')
        workflow.logger.info('Repair round %d: the repository\'s checks', attempt)
        self.progress.repairs.append({
            'attempt': attempt, 'target': 'implementation', 'tests': [],
            'summary': f"The repository's checks failed: {self._checks_summary()}"})
        self.progress.verification_status = 'repairing'
        await self._implement_service({
            'attempt': attempt, 'instructions': instructions,
            'checks': checks.get('excerpt') or '', 'test_failures': []})
        if self.progress.implementation_error:
            self.progress.verification_status = 'completed'
            return False
        return True

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
            self.progress.verification_error = with_cause(
                "The repository's tests could not be run against the changed service.", exc)
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
                self.progress.verification_error = with_cause(
                    'Service verification could not complete.', exc)
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
            self.progress.verification_error = with_cause(
                'The failed tests could not be diagnosed.', exc)
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
