from typing import Any, cast

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

from app.delivery.flow.base import FlowBase

with workflow.unsafe.imports_passed_through():
    from app.config import IMPLEMENTATION_TASK_QUEUE, REVIEW_TASK_QUEUE, ROUTING_TASK_QUEUE
    from app.review.models import (
        BASELINE_KEY, MAX_STAGE_REVIEW_ROUNDS, SCOPE_KEY, ReviewFinding, ReviewRound,
        correctable, earlier_findings, review_request,
    )
    from app.routing.models import (
        ROUTING_ERROR, ROUTING_ERROR_CHARS, SEAM_ERROR, SEAM_MISMATCH, RequirementRoute,
        added_seams,
    )

# Marks the workflow code that reviews the seam a change adds, and its regression tests
# once they fail without the fix, in rounds of their own before the next stage; deliveries
# recorded before it went on unreviewed.
SEAM_REVIEW_PATCH = 'seam-review'
REGRESSION_REVIEW_PATCH = 'regression-review'
# How many failures of each baseline run, and how much of each message, a regression
# review is shown.
BASELINE_FAILURES = 50
BASELINE_MESSAGE_CHARS = 1000
ROUTING_PATCH = 'routing'
COMPONENT_RUNS_PATCH = 'component-runs'
AUTO_REROUTE_PATCH = 'auto-reroute'
# How many times a change automatically bounces a test-author contract/stub gap back to
# routing before stopping to ask the user.
MAX_CONTRACT_GAP_REROUTES = 2


def routing_failure(exc: BaseException) -> str:
    """The reason the routing gave for failing, when it is one of its own; only those are
    shown, as an agent's or provider's error may carry credentials (NFR-2)."""
    return own_failure(exc, ROUTING_ERROR)


def own_failure(exc: BaseException, error_type: str) -> str:
    """The reason a stage gave for failing as an ApplicationError of `error_type`, the
    type of the failures it words itself; '' for any other failure."""
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ApplicationError) and cause.type == error_type:
            return str(cause.message or '')[:ROUTING_ERROR_CHARS]
        cause = cause.cause if isinstance(cause, (ActivityError, ChildWorkflowError,
                                                  ApplicationError)) else None
    return ''


class Change(FlowBase):
    """A change to an existing service, red to green: routing of its criteria, the
    seam they are observed at, and the reviews of the seam and the regression tests."""

    async def _change(self) -> None:
        """Change the repository's service red to green: the suite that can prove each
        criterion, regression tests, proof that they fail against the service as it is,
        then the fix and the whole suite."""
        self.progress.verification_status = 'waiting'
        self.progress.baseline_status = 'waiting'
        # No test is written for criteria no suite can observe, nor before the seam they
        # are observed at, when the code does not have it yet, is added.
        reroutes = 0
        auto_reroute = workflow.patched(AUTO_REROUTE_PATCH)
        while True:
            if not await self._route() or not await self._prepare_seam():
                self.progress.baseline_status = 'skipped'
                self.progress.verification_status = 'skipped'
                return
            await self._generate_tests()
            if (auto_reroute and reroutes < MAX_CONTRACT_GAP_REROUTES
                    and not self.progress.component_tests_error
                    and self.progress.routing_status == 'unobservable'
                    and self._has_reroutable_decline()):
                reroutes += 1
                workflow.logger.info(
                    'Test author reported a contract gap; re-routing automatically (%d/%d)',
                    reroutes, MAX_CONTRACT_GAP_REROUTES)
                self._prepare_contract_gap_reroute()
                continue
            break
        # A failed author, or one that found a criterion its suite cannot observe, leaves
        # nothing to hold against the service, whatever the other author wrote.
        if self.progress.component_tests_error or self.progress.routing_status not in (
                'routed', 'not_applicable'):
            self.progress.baseline_status = 'skipped'
            self.progress.verification_status = 'skipped'
            return
        if self.progress.component_tests:
            workflow.deprecate_patch(COMPONENT_RUNS_PATCH)
        # Nothing is fixed before the added tests are shown to fail without the fix, and
        # reviewed as the regression tests they are.
        if not await self._reproduce_bounded() or not await self._review_regression_tests():
            self.progress.verification_status = 'skipped'
            return
        # Retried tests are verified against the fix the delivery already has.
        if not self.progress.implementation:
            await self._implement_service()
        await self._verify_built()

    def _prepare_contract_gap_reroute(self) -> None:
        """Feed test-author contract/stub declines back into ObservabilityWorkflow."""
        self.state.declined_routes = {
            requirement: route for requirement, route in (self.progress.routing or {}).items()
            if route['source'] == 'tests'
        } or None
        self.progress.routing = None
        self.progress.routing_status = 'waiting'
        self.progress.routing_error = None
        self.progress.tests = None
        self.progress.tests_error = None
        self.progress.component_tests = None
        self.progress.component_tests_error = None
        self.progress.seam = None
        self.progress.seam_status = 'not_applicable'
        self.progress.seam_error = None
        self.progress.seam_reviews = []

    async def _route(self) -> bool:
        """Decide which suite can prove each criterion of the brief: true when every one
        has one. A routing that found one for each is kept for the retries of the tests."""
        if self.progress.routing_status == 'routed':
            return True
        workflow.deprecate_patch(ROUTING_PATCH)
        self.progress.routing_status = 'running'
        self.progress.routing = None
        self.progress.routing_error = None
        context = self._stage_context(None)
        if self.state.declined_routes:
            # What the test author lacked at the seams of the last routing.
            context['declined'] = self.state.declined_routes
        try:
            result = await self._child(
                'ObservabilityWorkflow', ROUTING_TASK_QUEUE, self._child_id('routing'),
                context)
        except ChildWorkflowError as exc:
            workflow.logger.warning('Routing failed: %s', exc.cause or exc)
            reason = routing_failure(exc)
            self.progress.routing_error = ('It could not be decided which tests can prove the '
                                           'acceptance criteria.' + (f' {reason}' if reason
                                                                     else ''))
            self.progress.routing_status = 'error'
            return False
        self.state.declined_routes = None
        # Whether the repository has parts beside the service, which the routing could
        # route to: what the user is told to rephrase a criterion as depends on it.
        self.state.routing_parts = bool(result.get('repository_parts'))
        routing = {requirement: cast(RequirementRoute, {**route, 'source': 'routing'})
                   for requirement, route in result['routes'].items()}
        self.progress.routing = routing
        self.progress.routing_status, self.progress.routing_error = self._judge_routes(routing)
        workflow.logger.info('Routing: %s', self.progress.routing_status)
        return self.progress.routing_status == 'routed'

    async def _prepare_seam(self) -> bool:
        """Add the seam criteria routed to the component tests are observed at, when the
        code does not have it yet: with no behaviour change, checked against the component
        contract, then reviewed and corrected in rounds of its own. True when the code has
        every seam the routing named. A seam added already is kept for the retries of the
        tests."""
        added = added_seams(self._component_routes())
        if not added:
            return True
        if self.progress.seam_status == 'prepared':
            return True
        self.progress.seam_status = 'running'
        self.progress.seam = None
        self.progress.seam_error = None
        self.progress.seam_reviews = []
        if not await self._add_seam(added):
            return False
        if workflow.patched(SEAM_REVIEW_PATCH) and not await self._review_seam(added):
            return False
        self.progress.seam_status = 'prepared'
        workflow.logger.info('Seam added: %s', sorted(added))
        return True

    async def _add_seam(self, added: dict[str, Any],
                        repair: dict[str, Any] | None = None) -> bool:
        """Have the seam's author add it, or with `repair` correct the review's findings
        about it; true when the code it published conforms to the component contract."""
        try:
            result = await self._child(
                'ImplementationWorkflow', IMPLEMENTATION_TASK_QUEUE,
                self._child_id('seam', repair['attempt'] if repair else 0),
                {**self._stage_context(repair), 'seam': added})
        except ChildWorkflowError as exc:
            workflow.logger.warning('Seam preparation failed: %s', exc.cause or exc)
            reason = own_failure(exc, SEAM_ERROR)
            self.progress.seam_error = (
                ('The review findings about the seam could not be corrected in the code or '
                 'built.' if repair else
                 'The seam the acceptance criteria are observed at could not be added to the '
                 'code or built.') + (f' {reason}' if reason else ''))
            self.progress.seam_status = 'error'
            return False
        if result.get('conformance_error'):
            self.progress.seam_error = f'{SEAM_MISMATCH} {result["conformance_error"]}'
            self.progress.seam_status = 'error'
            return False
        self.progress.seam = result
        return True

    async def _review_seam(self, added: dict[str, Any]) -> bool:
        """Review the seam before any test is written; while new findings remain, its
        author corrects them, the code is checked against the contract again and reviewed
        again. True when the delivery goes on with it."""
        async def correct(findings: list[ReviewFinding], number: int) -> bool:
            return await self._add_seam(
                added, review_request(findings, 'implementation', number))

        reviewed = await self._stage_review_rounds(
            'seam', self.progress.seam_reviews, correct, lambda: {'seam': added})
        if reviewed is None:
            self.progress.seam_error = 'The seam the change added could not be reviewed.'
            self.progress.seam_status = 'error'
            return False
        return reviewed

    async def _review_regression_tests(self) -> bool:
        """Review the regression tests once they were seen failing without the fix; while
        new findings remain, their authors correct them, each suite its own, the baseline
        runs again (with its own rounds) and the tests are reviewed again. True when the
        fix may be written."""
        self.progress.test_reviews = []
        if not workflow.patched(REGRESSION_REVIEW_PATCH):
            return True

        async def correct(findings: list[ReviewFinding], number: int) -> bool:
            self.progress.baseline_status = 'running'
            await self._generate_tests(
                review_request(findings, 'tests', number, before_fix=True))
            if self.progress.tests_error or self.progress.component_tests_error:
                self.progress.baseline_status = 'skipped'
                return False
            # Corrected tests that no longer fail without the fix prove nothing.
            return await self._reproduce_bounded()

        reviewed = await self._stage_review_rounds(
            'regression', self.progress.test_reviews, correct, lambda: {
                BASELINE_KEY: self._baseline_failures(),
                'contract_tests': bool(self.progress.tests)})
        if reviewed is None:
            self.progress.tests_error = 'The regression tests could not be reviewed.'
            return False
        return reviewed

    def _baseline_failures(self) -> dict[str, list[dict[str, Any]]]:
        """How the tests failed in the last baseline run of each suite, bounded."""
        runs = {'contract': self.progress.baseline,
                'component': self.progress.component_baseline}
        return {suite: [{**item, 'message': item['message'][:BASELINE_MESSAGE_CHARS]}
                        for item in run['test_failures'][:BASELINE_FAILURES]]
                for suite, run in runs.items() if run}

    async def _stage_review_rounds(self, scope: str, rounds: list[ReviewRound],
                                   correct: Any, extra: Any) -> bool | None:
        """Review one stage's output before the next stage starts, as the delivery's own
        review rounds do: while new findings an author can correct remain, `correct`
        them, and review again, at most MAX_STAGE_REVIEW_ROUNDS times. Each round knows
        the findings already corrected, and a finding reported again is not sent for
        another correction. Findings left go on with the delivery, whose own review sees
        them again. None when a review could not run, False when a correction failed."""
        earlier: list[ReviewFinding] = []
        for count in range(1, MAX_STAGE_REVIEW_ROUNDS + 1):
            findings = await self._stage_review(scope, rounds, earlier, extra())
            if findings is None:
                return None
            new = [item for item in findings if 'repeats' not in item and correctable(item)]
            if not new:
                return True
            if count == MAX_STAGE_REVIEW_ROUNDS:
                workflow.logger.info('%s review findings remain after %d rounds', scope,
                                     MAX_STAGE_REVIEW_ROUNDS)
                return True
            if not await correct(new, rounds[-1]['round']):
                return False
            earlier += new
        return True

    async def _stage_review(self, scope: str, rounds: list[ReviewRound],
                            earlier: list[ReviewFinding],
                            extra: dict[str, Any]) -> list[ReviewFinding] | None:
        """One round of a stage review, recorded in `rounds`; None when it could not run."""
        number = len(rounds) + 1
        context = {**self._coding_context(), SCOPE_KEY: scope, **extra}
        if earlier:
            context['previous_findings'] = earlier_findings(earlier)
        try:
            result = await self._child(
                'ReviewWorkflow', REVIEW_TASK_QUEUE, self._child_id(f'{scope}-review', number),
                context)
        except ChildWorkflowError as exc:
            workflow.logger.warning('The %s review failed: %s', scope, exc.cause or exc)
            return None
        findings: list[ReviewFinding] = list(result.get('findings') or [])
        rounds.append({'round': number, 'findings': findings})
        workflow.logger.info('%s review round %d: %d findings', scope, number, len(findings))
        return findings
