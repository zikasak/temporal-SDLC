import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from app.delivery.flow.base import FlowBase

with workflow.unsafe.imports_passed_through():
    from app.delivery.models import (
        CI_CHECK_INTERVAL, FOLLOW_UP_PREFIX, PULL_REQUEST_CHECK_INTERVAL, TASK_IDLE_TIMEOUT,
        DeliveryProgress,
    )
    from app.temporal.closing import end_cancelled

# Marks the workflow code that closes a flow carrying on a closed delivery once it has
# nothing left to do: no pull request of its own and no fix round to run, such as after
# answering a question or an unclear comment, rather than waiting TASK_IDLE_TIMEOUT.
FOLLOW_UP_CLOSES_PATCH = 'follow-up-closes'
# How long such a flow, started for a mention, waits for the mention to be handed to it.
FOLLOW_UP_HANDOVER_TIMEOUT = timedelta(hours=1)


class PullRequestWait(FlowBase):
    """How a finished delivery waits: for the user's next decision, a
    mention's fix round, or its pull request being merged or closed."""

    @workflow.signal
    def pull_request_closed(self, number: int, merged: bool) -> None:
        """Signal: GitHub reported (a pull_request webhook event) that pull request
        `number` of the delivery was closed, merged when `merged` is true. A waiting
        delivery whose pull request it is stops waiting at once: it completes when the
        pull request was merged and ends as cancelled when it was closed without being
        merged, as `run` describes. A signal about another pull request changes
        nothing."""
        pull = self.progress.pull_request
        if not pull or type(number) is not int or pull.get('number') != number:
            return
        if self._closed is None or self._closed[0] != number:
            self._closed = (number, bool(merged))
            workflow.logger.info('Pull request #%d was %s', number,
                                 'merged' if merged else 'closed without merging')

    @workflow.signal
    def pull_request_conflicted(self, number: int) -> None:
        """Signal 'pull_request_conflicted', arg [number]: GitHub reported (a pull_request
        webhook event) that pull request `number` may conflict with its base branch. A
        waiting delivery or onboarding whose pull request it is runs the activity
        'pull_request_conflicts' at once instead of at the next check interval; a signal
        about another pull request, or while no pull request exists, changes nothing."""
        pull = self.progress.pull_request
        if not pull or type(number) is not int or pull.get('number') != number:
            return
        self._conflict_requested = True

    @workflow.signal
    def check_pull_request(self) -> None:
        """Check the pull request's state now rather than at the next interval, such as
        when the workers start; a run that began to wait before deliveries checked their
        pull request moves on to one that does."""
        self._check_requested = True

    def _closing(self) -> bool | None:
        """Whether the delivery's current pull request was reported merged (true) or
        closed without merging (false); None while it is not known to be closed."""
        pull = self.progress.pull_request
        if self._closed is None or not pull or pull.get('number') != self._closed[0]:
            return None
        return self._closed[1]

    def _checks_pull_request(self) -> bool:
        pull = self.progress.pull_request
        repository = self.spec.repository
        return bool(pull and pull.get('number') and repository
                    and repository.get('full_name'))

    async def _check_pull_request(self) -> None:
        """Ask GitHub how the pull request stands; a failed check changes nothing."""
        pull, repository = self.progress.pull_request, self.spec.repository
        if not self._checks_pull_request() or pull is None or repository is None:
            return
        number = pull['number']
        try:
            current = await workflow.execute_activity(
                'pull_request_state',
                args=[repository['full_name'], number],
                result_type=dict, start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Pull request state not read: %s', exc.cause or exc)
            return
        if not isinstance(current, dict):
            return
        merged = current.get('merged') is True
        if (merged or current.get('state') == 'closed') and self._closed is None:
            workflow.logger.info('Pull request #%d found %s', number,
                                 'merged' if merged else 'closed without merging')
            self._closed = (number, merged)
        elif current.get('state') == 'open' and not merged:
            # An open pull request may conflict with its base branch, which moved on.
            self._conflict_requested = False
            await self._check_base_conflicts()

    async def _wait_for_pull_request(self) -> bool:
        """Wait for the user's next decision, a mention's fix round or the end of the
        pull request, checking its state every PULL_REQUEST_CHECK_INTERVAL. True when a
        next run is to start, false when the delivery completes; a pull request closed
        without merging ends the run as cancelled."""
        # A recorded decision about a delivery that was pushed without a pull request, such
        # as an acceptance of its findings after it closed, has nothing to wait for either.
        pushed = self._pushed or bool(
            self.state.record_only and self.progress.publication
            and self.progress.publication_status == 'completed'
            and self.progress.pull_request_status == 'error')
        if pushed and not self.progress.pull_request and not self._fix_pending():
            # Pushed, but no pull request could be opened: nothing to wait for.
            workflow.logger.info('Pushed without a pull request, closing')
            self.state.waiting = False
            await workflow.wait_condition(workflow.all_handlers_finished)
            if not self._fix_pending():
                self._hide_requirement_mapping(drop=True)
                return False
            self.state.waiting = True
            self._start_queued()
        if (self._next is None and self._is_follow_up() and not self.progress.pull_request
                and workflow.patched(FOLLOW_UP_CLOSES_PATCH)):
            if not await self._follow_up_continues():
                self._hide_requirement_mapping(drop=True)
                return False
        # Questions about the requirements a merge of the base branch left without a
        # counterpart are asked now, while everything below goes on.
        self._show_requirement_mapping()
        deadline = workflow.now() + TASK_IDLE_TIMEOUT
        # While the pushed commit's CI runs, its checks are read every CI_CHECK_INTERVAL,
        # the first time at once, and the pull request's state every
        # PULL_REQUEST_CHECK_INTERVAL as before.
        watching = self._ci_watching()
        next_ci = workflow.now()
        next_pull_request = workflow.now() + PULL_REQUEST_CHECK_INTERVAL
        while True:
            if self._next is not None:
                self._hide_requirement_mapping(drop=False)
                return True
            merged = self._closing()
            if merged is not None:
                self._hide_requirement_mapping(drop=True)
                await self._close(merged)
                return False
            if self._mapping_due():
                await self._settle_requirement_mapping()
                continue
            if watching and workflow.now() >= next_ci:
                await self._check_ci()
                watching = self._ci_pending()
                next_ci = workflow.now() + CI_CHECK_INTERVAL
                continue
            remaining = deadline - workflow.now()
            if remaining <= timedelta(0):
                workflow.logger.info('Delivery idle, closing')
                self.state.waiting = False
                await workflow.wait_condition(workflow.all_handlers_finished)
                if not self._fix_pending():
                    self._hide_requirement_mapping(drop=True)
                    return False
                workflow.logger.info('Fix request queued while closing; continuing')
                self.state.waiting = True
                self._start_queued()
                deadline = workflow.now() + TASK_IDLE_TIMEOUT
                continue
            checking = self._checks_pull_request()
            timeout = min(remaining, PULL_REQUEST_CHECK_INTERVAL) if checking else remaining
            if watching:
                timeout = min(remaining, next_ci - workflow.now())
                if checking:
                    timeout = min(timeout, next_pull_request - workflow.now())
            if self._mapping_questions and self._mapping_deadline is not None:
                timeout = max(min(timeout, self._mapping_deadline - workflow.now()),
                              timedelta(seconds=1))
            try:
                await workflow.wait_condition(
                    lambda: (self._next is not None or self._closing() is not None
                             or self._check_requested or self._conflict_requested
                             or self._mapping_due()),
                    timeout=timeout)
            except asyncio.TimeoutError:
                pass
            if self._next is not None or self._closing() is not None or self._mapping_due():
                continue
            if self._conflict_requested:
                # GitHub reported that the pull request may conflict with its base branch.
                self._conflict_requested = False
                if workflow.now() < deadline:
                    await self._check_base_conflicts()
                continue
            if watching:
                if self._check_requested:
                    # A requested check reads the CI too.
                    next_ci = workflow.now()
                if self._check_requested or (checking
                                             and workflow.now() >= next_pull_request):
                    self._check_requested = False
                    next_pull_request = workflow.now() + PULL_REQUEST_CHECK_INTERVAL
                    if workflow.now() < deadline:
                        await self._check_pull_request()
                continue
            if self._check_requested or checking:
                self._check_requested = False
                if workflow.now() < deadline:
                    await self._check_pull_request()

    def _is_follow_up(self) -> bool:
        """Whether this delivery is a flow that carries on a closed one."""
        return (self.spec.session_id or '').startswith(FOLLOW_UP_PREFIX)

    async def _follow_up_continues(self) -> bool:
        """For a flow carrying on a closed delivery without a pull request of its own:
        wait for the mention it was started for to be handed to it and handled. True when
        a fix round is to run next; false when it has nothing left to do, such as after
        answering a question, an unclear comment or a fix request it cannot take, or after
        a fix round that pushed nothing, so that it completes rather than staying open."""
        try:
            await workflow.wait_condition(
                lambda: self._next is not None or self._handed_over,
                timeout=FOLLOW_UP_HANDOVER_TIMEOUT)
        except asyncio.TimeoutError:
            pass
        if self._next is not None:
            return True
        # Its mentions are classified and answered, or queued as fix rounds.
        await workflow.wait_condition(
            lambda: self._next is not None or workflow.all_handlers_finished())
        if self._next is not None:
            return True
        workflow.logger.info('Nothing left to do for the follow-up flow, closing')
        self.state.waiting = False
        await workflow.wait_condition(workflow.all_handlers_finished)
        if self._fix_pending():
            self.state.waiting = True
            self._start_queued()
        return self._next is not None

    async def _close(self, merged: bool) -> DeliveryProgress:
        """End the delivery as its pull request ended: completed when it was merged,
        cancelled when it was closed without merging. The open-review-findings flag stays
        as it is, for the user to accept the findings or give instructions about them."""
        self.state.waiting = False
        await workflow.wait_condition(workflow.all_handlers_finished)
        await self._close_flags()
        if merged:
            workflow.logger.info('Pull request merged, delivery completed')
            return self.progress
        workflow.logger.info('Pull request closed without merging, delivery cancelled')
        await end_cancelled('The pull request was closed without being merged.')
