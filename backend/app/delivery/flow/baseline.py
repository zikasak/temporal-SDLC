import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError

from app.delivery.flow.stages import MAX_TRACKED, VERIFICATION_ATTEMPTS

with workflow.unsafe.imports_passed_through():
    from app.config import VERIFICATION_TASK_QUEUE
    from app.delivery.models import (
        bound_request, component_run, hung_tests, judge_baseline, reproduce_request,
        unreproducing_tests,
    )
    from app.github.checkout import SNAPSHOT_DIR

# Rounds in which the authors of a change's tests bound the waits of the tests the runner
# stopped at its per-test time limit in the baseline run, each run against it again.
MAX_BOUND_ROUNDS = 2
# Marks the workflow code that sends such tests back to their authors; deliveries
# recorded before it stopped at that baseline.
BOUND_HUNG_PATCH = 'bound-hung-tests'
# Rounds in which the authors of a bugfix's regression tests correct those the baseline
# run showed written wrong, such as tests that pass without the fix, each run again.
MAX_REPRODUCE_ROUNDS = 1
# Marks the workflow code that sends such tests back; deliveries recorded before it
# stopped at that baseline.
REPRODUCE_PATCH = 'reproduce-baseline-tests'


class Baseline:
    """The baseline run of a change's tests against the code as the repository holds
    it, with the rounds in which their authors correct tests it showed written wrong."""

    async def _reproduce(self, repair: int = 0) -> bool:
        """Run the changed suite against the service as the repository holds it: true
        when every added test fails there and every existing test passes, but for
        those a repair corrected. A change with component tests runs them too, and is
        judged on both suites."""
        if self.progress.component_tests:
            return await self._reproduce_components(repair)
        if not self.progress.tests or self.progress.tests_error:
            self.progress.baseline_status = 'skipped'
            return False
        self.progress.baseline_status = 'running'
        self.progress.baseline = None
        self.progress.baseline_error = None
        added, changed = (self.progress.tests[key] for key in ('added_tests', 'changed_tests'))
        try:
            image = await self._build_baseline()
            self.progress.baseline = await self._child(
                'VerificationWorkflow', VERIFICATION_TASK_QUEUE,
                self._child_id('baseline', repair), image['image_id'],
                self.progress.tests['image_id'], self.spec.language, [*added, *changed])
        except (ActivityError, ChildWorkflowError) as exc:
            workflow.logger.warning('Baseline run failed: %s', exc.cause or exc)
            self.progress.baseline_error = ('The tests could not be run against the service as '
                                            'the repository holds it.')
            self.progress.baseline_status = 'error'
            return False
        self.progress.baseline_status, self.progress.baseline_error = judge_baseline(
            self.progress.baseline, added, changed, seam=self._seam_prepared())
        workflow.logger.info('Baseline run: %s', self.progress.baseline_status)
        return self.progress.baseline_status == 'reproduced'

    async def _reproduce_bounded(self, repair: int = 0,
                                 findings: list[dict[str, Any]] | None = None) -> bool:
        """The baseline run, with rounds in which the authors of a change's tests correct
        those the run showed written wrong, changing them alone, and the baseline runs
        again. While the runner stopped tests at its per-test time limit, at most
        MAX_BOUND_ROUNDS rounds bound their waits: such a test waited for what the code
        does not do yet without saying what. Then, when added tests passed without the
        fix or did not run, at most MAX_REPRODUCE_ROUNDS rounds rewrite them: such a test
        does not reproduce the problem. A baseline that fails for anything else, such as
        failing existing tests, proves nothing the authors can correct. `repair` numbers
        the review round whose correction of the tests this baseline holds, and
        `findings` are the review findings the test authors corrected then."""
        bounds = reproductions = 0
        while True:
            if await self._reproduce(repair):
                return True
            hung = self._hung_suites()
            if hung:
                if bounds == MAX_BOUND_ROUNDS or not workflow.patched(BOUND_HUNG_PATCH):
                    return False
                bounds += 1
                workflow.logger.info('Bound round %d: %s', bounds, ', '.join(
                    f'{suite} {len(tests)}' for suite, tests in hung.items()))
                requests = {suite: bound_request(tests, bounds)
                            for suite, tests in hung.items()}
            else:
                wrong = self._unreproducing_suites()
                if (not wrong or reproductions == MAX_REPRODUCE_ROUNDS
                        or not workflow.patched(REPRODUCE_PATCH)):
                    return False
                reproductions += 1
                workflow.logger.info('Reproduce round %d: %s', reproductions, ', '.join(
                    f'{suite} {sum(map(len, tests.values()))}' for suite, tests in wrong.items()))
                requests = {suite: reproduce_request(tests, reproductions, findings)
                            for suite, tests in wrong.items()}
            await self._rewrite_tests(requests)
            if self.progress.routing_status not in ('routed', 'not_applicable'):
                # The author found a criterion its suite cannot observe after all.
                self.progress.baseline_status = 'skipped'
                return False

    async def _rewrite_tests(self, requests: dict[str, dict[str, Any]]) -> None:
        """Have the author of each suite correct the tests its request names."""
        self.progress.baseline_status = 'running'
        components = bool(self.progress.component_tests)
        await asyncio.gather(*(
            self._generate_tests(request, suite if components else None)
            for suite, request in requests.items()))

    def _hung_suites(self) -> dict[str, list[Any]]:
        """The tests of each suite the runner stopped at its per-test time limit in the
        last baseline run, by the suite whose author corrects them."""
        # The repository's own contract tests, run when no author wrote any, have none.
        hung = {'contract': hung_tests(self.progress.baseline) if self.progress.tests else [],
                'component': hung_tests(self.progress.component_baseline)}
        return {suite: tests for suite, tests in hung.items() if tests}

    def _unreproducing_suites(self) -> dict[str, dict[str, list[str]]]:
        """The added tests of each suite the last baseline run showed written wrong, by
        the suite whose author corrects them; none while a run of either suite proves
        nothing for another reason, which correcting the added tests would not change."""
        tests, components = self.progress.tests, self.progress.component_tests
        runs = []
        if self.progress.baseline is not None:
            # The repository's own contract tests, run when no author wrote any, add none.
            runs.append(('contract', self.progress.baseline,
                         tests['added_tests'] if tests else [],
                         tests['changed_tests'] if tests else []))
        if components and self.progress.component_baseline is not None:
            runs.append(('component', self.progress.component_baseline,
                         components['added_tests'], components['changed_tests']))
        suites = {}
        for suite, result, added, changed in runs:
            wrong = unreproducing_tests(result, added, changed)
            if wrong is None:
                return {}
            if wrong:
                suites[suite] = wrong
        return suites

    async def _build_baseline(self) -> dict[str, Any]:
        if self._seam_prepared():
            # The service with the seam the change added was built when it was added.
            return {'image_id': self.progress.seam['image_id']}
        return await workflow.execute_activity(
            'build_baseline', args=[self.spec.session_id, self.spec.language],
            task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    async def _contract_suite(self) -> tuple[str, str, list[str], list[str]] | None:
        """The contract tests a change with component tests runs: its author's suite, with
        the tests it added and the corrected ones, or, when the change gave it no
        criterion, the repository's own suite as it is, which the change has to keep
        passing. None when the repository has no contract tests either."""
        tests = self.progress.tests
        if tests:
            return tests['image_id'], 'tests', tests['added_tests'], tests['changed_tests']
        if not self._repository_tests:
            return None
        image = await workflow.execute_activity(
            'build_repository_tests', args=[self.spec.session_id, self.spec.language],
            task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return image['image_id'], SNAPSHOT_DIR, [], []

    async def _run_components(self, runner_image: str, tracked: list[str]) -> dict[str, Any]:
        """Run the component tests once, and once more when the run could not complete,
        as a run of the contract tests is; each test of `tracked` is reported singly."""
        for attempt in range(1, VERIFICATION_ATTEMPTS + 1):
            result = await workflow.execute_activity(
                'run_component_suite',
                args=[self.spec.session_id, runner_image, self.spec.language, tracked],
                task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
                start_to_close_timeout=timedelta(minutes=15),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            if result['status'] != 'error':
                break
            workflow.logger.warning('Component test run %d could not complete: %s', attempt,
                                    result['error'])
        return result

    async def _reproduce_components(self, repair: int) -> bool:
        """The baseline of a change with component tests: its contract tests, if it has
        any, against the service as the repository holds it, and its component tests,
        built by their author with the code as the repository holds it."""
        if self.progress.tests_error or self.progress.component_tests_error:
            self.progress.baseline_status = 'skipped'
            return False
        self.progress.baseline_status = 'running'
        self.progress.baseline = None
        self.progress.baseline_error = None
        self.progress.component_baseline = None
        components = self.progress.component_tests
        tracked = [*components['added_tests'], *components['changed_tests']]
        if len(tracked) > MAX_TRACKED:
            # The baseline judges every added test on its own, so none may go unreported.
            self.progress.baseline_error = (
                f'The component tests added {len(tracked)} tests, more than one baseline run '
                f'reports on singly ({MAX_TRACKED}), so the baseline cannot prove that each '
                'fails without the fix. Write fewer regression tests.')
            self.progress.baseline_status = 'error'
            return False
        added, changed = [], []
        try:
            suite = await self._contract_suite()
            if suite:
                test_image, suite_dir, added, changed = suite
                image = await self._build_baseline()
                self.progress.baseline = await self._child(
                    'VerificationWorkflow', VERIFICATION_TASK_QUEUE,
                    self._child_id('baseline', repair), image['image_id'], test_image,
                    self.spec.language, [*added, *changed], suite_dir)
            self.progress.component_baseline = await self._run_components(
                components['image_id'], tracked)
        except (ActivityError, ChildWorkflowError) as exc:
            workflow.logger.warning('Baseline run failed: %s', exc.cause or exc)
            self.progress.baseline_error = ('The tests could not be run against the code as '
                                            'the repository holds it.')
            self.progress.baseline_status = 'error'
            return False
        self.progress.baseline_status, self.progress.baseline_error = judge_baseline(
            self.progress.baseline, added, changed, component_run(
                self.progress.component_baseline, components['added_tests'],
                components['changed_tests']), seam=self._seam_prepared())
        workflow.logger.info('Baseline run: %s', self.progress.baseline_status)
        return self.progress.baseline_status == 'reproduced'
