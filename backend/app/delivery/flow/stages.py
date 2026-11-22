import asyncio
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ChildWorkflowError

from app.delivery.flow.base import FlowBase

with workflow.unsafe.imports_passed_through():
    from app.delivery.causes import with_cause
    from app.config import (
        COMPONENT_TEST_GENERATION_TASK_QUEUE, IMPLEMENTATION_TASK_QUEUE,
        TEST_GENERATION_TASK_QUEUE,
    )
    from app.review.models import ReviewFinding, for_suite
    from app.routing.models import RequirementRoute, RoutingStatus, judge_routes

# Patches whose old code paths are gone; histories recorded with their markers replay
# through workflow.deprecate_patch, called where workflow.patched was.
RESTORE_KEPT_PATCH = 'restore-kept-tests'
COMPONENT_TESTS_PATCH = 'component-tests'
COMPONENT_REPAIRS_PATCH = 'component-repairs'
# Marks the workflow code that takes criteria routed to a page of a part's web client
# (the `browser` route) to the component author and its runners. A delivery whose
# history was recorded before it stops at such a route as `unsupported`.
BROWSER_TESTS_PATCH = 'browser-tests'
# Marks the workflow code that sends a review finding reported again after its correction
# for another one, up to MAX_FINDING_CORRECTIONS; tells the analysis of failed checks the
# findings being corrected, and leaves a finding it finds failing tests no author may
# change contradict open; and releases the repository's kept tests a finding names.
FINDING_CONFLICTS_PATCH = 'review-finding-conflicts'
# Runs of one verification whose result is an infrastructure error, not a verdict.
VERIFICATION_ATTEMPTS = 2
# An API change may add more tests than one run reports on singly; the run still
# answers for every test of the suite.
MAX_TRACKED = 1000


def restore_finding(kept: list[str],
                    reason: str = 'Verification rejected the tests.') -> ReviewFinding:
    """The finding that sends the test author back to the repository's `kept` tests it
    changed or removed although the changed service still passes them."""
    return {'summary': (
        'These tests of the repository were changed or removed, but the changed service '
        f'still passes them as the repository holds them: {", ".join(kept)}. A test the '
        'contract still supports has to stay exactly as the repository holds it: put '
        'each one back as the `restore` entry gives it.'),
        'target': 'tests', 'reason': reason}


class Stages(FlowBase):
    """The stage authors a delivery runs as child workflows, and the context each
    is given: the implementation, the contract tests and the component tests."""

    def _child_id(self, stage: str, repair: int = 0) -> str:
        child_id = f'{stage}-{self.spec.session_id}'
        if self.state.retry_stage or self.state.instructions or self.state.mention is not None:
            child_id += f'-{workflow.info().run_id}'
        if repair:
            child_id += f'-repair-{repair}'
        count = self._child_ids.get(child_id, 0) + 1
        self._child_ids[child_id] = count
        # Review rounds verify again, with repair rounds of their own.
        return child_id if count == 1 else f'{child_id}-{count}'

    def _author_id(self, stage: str, repair: dict[str, Any] | None) -> str:
        """The child workflow ID of an author's run, or of its repair round."""
        return self._child_id(self._stage_name(stage, repair),
                              repair['attempt'] if repair else 0)

    async def _child(self, name: str, task_queue: str, child_id: str,
                     *args: Any) -> dict[str, Any]:
        """Run one stage of this session as a child workflow and return its result."""
        return await workflow.execute_child_workflow(
            name, args=[self.spec.session_id, *args], id=child_id, task_queue=task_queue,
            result_type=dict)

    @staticmethod
    def _stage_name(stage: str, repair: dict[str, Any] | None) -> str:
        """Corrections of review findings are told apart from repairs of failed tests."""
        if repair and 'review' in repair:
            return f'{stage}-review'
        # Bounding the waits of hung tests, and rewriting tests that did not fail without
        # the fix, are told apart from repairs of failed ones.
        if repair and repair.get('bound'):
            return f'{stage}-bounds'
        return f'{stage}-reproduce' if repair and repair.get('reproduce') else stage

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
        if self.spec.onboarding:
            # The contract was drafted from the repository's existing code.
            context['onboarding'] = True
        if self.spec.architecture:
            # The task's top-level architecture, which the code follows; the agents never
            # change it.
            context['architecture'] = self.spec.architecture
        component = self._component_routes()
        if component:
            # The criteria proved at seams of the code: the fix keeps their stubs'
            # signatures, and the diagnosis and the review read their tests.
            context['component_routes'] = component
        if self._seam_prepared():
            # The seam was added first: the fix starts from the code with it, and the
            # component tests are built with that code.
            context['seam_prepared'] = True
        return context

    def _stage_context(self, repair: dict[str, Any] | None) -> dict[str, Any]:
        context = self._coding_context()
        if self.spec.checkout:
            # The authors then work inside the session's checkout of this repository.
            context['repository'] = self.spec.repository
        if repair:
            context['repair'] = repair
        return context

    def _component_routes(self) -> dict[str, Any]:
        """The criteria routed to a seam, with their reasons and seams, and those routed to
        a page of a part's web client, with their pages: the component suite proves both."""
        return {requirement: route for requirement, route in (self.progress.routing or {}).items()
                if route['route'] in ('component', 'browser')}

    def _seam_prepared(self) -> bool:
        return self.progress.seam_status == 'prepared' and self.progress.seam is not None

    def _judge_routes(self, routing: dict[str, RequirementRoute],
                      ) -> tuple[RoutingStatus, str | None]:
        if any(item['route'] == 'component' for item in routing.values()):
            workflow.deprecate_patch(COMPONENT_TESTS_PATCH)
        browser = [requirement for requirement, item in routing.items()
                   if item['route'] == 'browser']
        if browser and not workflow.patched(BROWSER_TESTS_PATCH):
            return 'unsupported', (
                f'The acceptance criteria {", ".join(browser)} were routed to browser tests, '
                'which this run of the delivery, started before they existed, cannot run. '
                'Retry the tests to route them again with browser tests.')
        return judge_routes(routing, self.state.routing_parts)

    def _declined(self, unobservable: list[dict[str, str]]) -> None:
        """Record criteria a test author found its suite cannot observe."""
        routing = dict(self.progress.routing or {})
        for item in unobservable:
            # The seam it was routed to, if any, is kept: a new routing is told of it.
            earlier = routing.get(item['requirement_id'])
            seam = earlier.get('seam') if earlier else None
            page = earlier.get('page') if earlier else None
            route = RequirementRoute(route='unobservable', reason=item['reason'], source='tests')
            if seam:
                route['seam'] = seam
            if page:
                route['page'] = page
            routing[item['requirement_id']] = route
        self.progress.routing = routing
        self.progress.routing_status, self.progress.routing_error = self._judge_routes(routing)
        workflow.logger.info('The test author found %d criteria unobservable',
                             len(unobservable))

    async def _implement_service(self, repair: dict[str, Any] | None = None) -> None:
        try:
            self.progress.implementation = await self._child(
                'ImplementationWorkflow', IMPLEMENTATION_TASK_QUEUE,
                self._author_id('implementation', repair), self._stage_context(repair))
            if self.spec.onboarding:
                self.progress.existing_code_changed = bool(
                    self.progress.implementation.get('code_changed'))
        except ChildWorkflowError as exc:
            workflow.logger.warning("Service implementation failed: %s", exc.cause or exc)
            self.progress.implementation_error = with_cause(
                "The service could not be implemented or built.", exc)

    async def _generate_tests(self, repair: dict[str, Any] | None = None,
                              suite: str | None = None) -> None:
        """Write the tests: the contract tests, and, for the first run of a change's tests
        with criteria routed to a seam, the component tests beside them. The contract
        tests' author is then given the criteria routed to it, and not run when there are
        none. A repair of a change with component tests runs the author of `suite`, or,
        for review findings, of each suite they concern."""
        if repair and self.progress.component_tests:
            workflow.deprecate_patch(COMPONENT_REPAIRS_PATCH)
            await self._repair_suites(repair, suite)
            return
        component = {} if repair else self._component_routes()
        routes = {requirement: {'route': route['route'], 'reason': route['reason']}
                  for requirement, route in (self.progress.routing or {}).items()
                  if route['route'] == 'contract'} if component else None
        authors = [self._generate_component_tests(component)] if component else []
        if routes or not component:
            authors.append(self._generate_contract_tests(repair, routes))
        await asyncio.gather(*authors)

    async def _repair_suites(self, repair: dict[str, Any], suite: str | None) -> None:
        """Have the authors of a change with component tests correct their suites: the
        one a diagnosis named, or those review findings concern, given only their own
        findings. A finding about no file concerns both, as do the user's instructions."""
        review = repair.get('review')
        if not suite and not review:
            # A diagnosis names the suite; one recorded without it condemns the failed one.
            component = self.progress.component_verification
            suite = 'component' if component and component['status'] == 'failed' else 'contract'
        if suite == 'contract' and not self.progress.tests:
            self.progress.tests_error = ("The repository's own contract tests are not "
                                         'repaired: no author of this change wrote any.')
            return
        suites = [suite] if suite else ['component', *(['contract'] if self.progress.tests else [])]
        authors = []
        for name in suites:
            own = repair
            if review and not suite:
                findings = for_suite(review['findings'], name)
                if not findings and not review.get('instructions'):
                    continue
                own = {**repair, 'review': {**review, 'findings': findings}}
            if name == 'component':
                authors.append(self._generate_component_tests(self._component_routes(), own))
            else:
                routes = {requirement: {'route': route['route'], 'reason': route['reason']}
                          for requirement, route in (self.progress.routing or {}).items()
                          if route['route'] == 'contract'}
                authors.append(self._generate_contract_tests(own, routes))
        await asyncio.gather(*authors)

    async def _generate_component_tests(self, routes: dict[str, Any],
                                        repair: dict[str, Any] | None = None) -> None:
        """Write the component tests, or with `repair` correct them; a failed repair keeps
        the suite it was to correct, and records its error."""
        if not repair:
            self.progress.component_tests = None
        self.progress.component_tests_error = None
        try:
            tests = await self._child(
                'ComponentTestGenerationWorkflow', COMPONENT_TEST_GENERATION_TASK_QUEUE,
                self._author_id('component-test-generation', repair),
                {**self._stage_context(repair), 'routes': routes})
        except ChildWorkflowError as exc:
            workflow.logger.warning('Component test generation failed: %s', exc.cause or exc)
            self.progress.component_tests_error = with_cause(
                'The component tests could not be repaired or built with the code.' if repair
                else 'The component tests could not be generated or built with the code.', exc)
            return
        if 'unobservable' in tests:
            self._declined(tests['unobservable'])
            return
        self.progress.component_tests = tests

    async def _generate_contract_tests(self, repair: dict[str, Any] | None,
                                       routes: dict[str, Any] | None) -> None:
        # Tests put back as the repository holds them were seen passing so: they stay so.
        restore = (repair or {}).get('review', {}).get('restore') or []
        if restore:
            self.state.kept_tests = sorted({*self.state.kept_tests, *restore})
        context = self._stage_context(repair)
        if routes:
            # The criteria the contract tests prove, when the component tests prove others.
            context['routes'] = routes
        keep = [case for case in self.state.kept_tests if case not in self.state.released_tests]
        if keep and self.spec.checkout:
            # The repository's tests the changed service was seen to pass as they are.
            context['keep'] = keep
        if self.state.released_tests and self.spec.checkout:
            # Those of them a review finding found wrong: the author may change them.
            context['released'] = list(self.state.released_tests)
        try:
            tests = await self._child(
                'TestGenerationWorkflow', TEST_GENERATION_TASK_QUEUE,
                self._author_id('test-generation', repair), context)
        except ChildWorkflowError as exc:
            workflow.logger.warning("Service test generation failed: %s", exc.cause or exc)
            self.progress.tests_error = with_cause(
                "The service tests could not be generated or built.", exc)
            return
        if 'unobservable' in tests:
            # No suite was written: the delivery stops, as when the routing found these.
            self._declined(tests['unobservable'])
            return
        self.progress.tests = tests
