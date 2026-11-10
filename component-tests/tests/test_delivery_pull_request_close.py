"""A delivery that waits after its push closes when its pull request is merged or closed.

Seam: app.delivery.workflows.DeliveryWorkflow, run in-process on a time-skipping Temporal
test server with fakes of the activities it calls by their registered names.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator, Callable

from temporalio import activity
from temporalio.client import WorkflowExecutionStatus, WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from app.delivery.models import PULL_REQUEST_CHECK_INTERVAL, DeliveryState, delivery_id
from app.delivery.workflows import DeliveryWorkflow
from helpers import (
    architecture_only_state, delivery_state, make_worker, unique_queue, wait_closed,
    wait_started, wait_until,
)

OPEN = {"state": "open", "merged": False}
MERGED = {"state": "closed", "merged": True}
CLOSED_UNMERGED = {"state": "closed", "merged": False}

SLACK = timedelta(minutes=1)


class PullRequestChecks:
    """A fake of the 'pull_request_state' activity: records its calls and answers as told."""

    def __init__(self, answer: Callable[[int], dict[str, Any]]) -> None:
        self.calls: list[tuple[str, int]] = []
        self._answer = answer

    def activity(self) -> Callable[..., Any]:
        checks = self

        @activity.defn(name="pull_request_state")
        async def pull_request_state(full_name: str, number: int) -> dict[str, Any]:
            checks.calls.append((full_name, number))
            return checks._answer(len(checks.calls))

        return pull_request_state


def always(answer: dict[str, Any]) -> Callable[[int], dict[str, Any]]:
    return lambda _call: answer


@asynccontextmanager
async def waiting_delivery(
    env: WorkflowEnvironment, state: DeliveryState, checks: PullRequestChecks,
    extra: tuple[Callable[..., Any], ...] = (),
) -> AsyncIterator[WorkflowHandle[Any, Any]]:
    """Run DeliveryWorkflow from `state` on a worker with the fake check; yield its handle."""
    queue = unique_queue()
    worker = make_worker(env.client, queue, [DeliveryWorkflow], [checks.activity(), *extra])
    async with worker:
        handle = await env.client.start_workflow(
            DeliveryWorkflow.run, state, id=delivery_id(state.spec.session_id), task_queue=queue,
        )
        try:
            yield handle
        finally:
            try:
                await handle.terminate("test finished")
            except Exception:
                pass


async def lifetime(handle: WorkflowHandle[Any, Any]) -> timedelta:
    """How long the execution ran, on the Temporal server's (time-skipping) clock."""
    desc = await handle.describe()
    assert desc.close_time is not None
    return desc.close_time - desc.start_time


class TestMergeOrCloseFoundByTheCheck:
    async def test_merged_pull_request_completes_the_delivery_within_one_interval(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        checks = PullRequestChecks(always(MERGED))
        async with waiting_delivery(skipping_env, delivery_state(), checks) as handle:
            status = await wait_closed(handle, "the delivery did not stop waiting for a merged pull request")
            assert status == WorkflowExecutionStatus.COMPLETED
            assert checks.calls[:1] == [("acme/widgets", 7)]
            assert await lifetime(handle) <= PULL_REQUEST_CHECK_INTERVAL + SLACK

    async def test_pull_request_closed_without_merge_cancels_the_delivery(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        checks = PullRequestChecks(always(CLOSED_UNMERGED))
        async with waiting_delivery(skipping_env, delivery_state(), checks) as handle:
            status = await wait_closed(handle, "the delivery did not stop waiting for a closed pull request")
            assert status == WorkflowExecutionStatus.CANCELED
            assert checks.calls[:1] == [("acme/widgets", 7)]
            assert await lifetime(handle) <= PULL_REQUEST_CHECK_INTERVAL + SLACK

    async def test_open_pull_request_keeps_waiting_and_is_checked_every_interval(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        answers = {"now": OPEN}
        checks = PullRequestChecks(lambda _call: answers["now"])
        async with waiting_delivery(skipping_env, delivery_state(), checks) as handle:
            await wait_started(handle)
            await skipping_env.sleep(PULL_REQUEST_CHECK_INTERVAL / 2)
            assert checks.calls == [], "the first check comes one interval after the delivery began to wait"
            await skipping_env.sleep(PULL_REQUEST_CHECK_INTERVAL * 2)
            await wait_until(lambda: len(checks.calls) >= 2, "the pull request was not checked every interval")
            assert set(checks.calls) == {("acme/widgets", 7)}
            assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING

            answers["now"] = MERGED
            status = await wait_closed(handle, "the delivery did not close after the pull request was merged")
            assert status == WorkflowExecutionStatus.COMPLETED

    async def test_failed_check_changes_nothing_and_the_next_interval_checks_again(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        def answer(call: int) -> dict[str, Any]:
            if call == 1:
                raise ApplicationError("GitHub is unavailable", non_retryable=True)
            return MERGED

        checks = PullRequestChecks(answer)
        async with waiting_delivery(skipping_env, delivery_state(), checks) as handle:
            status = await wait_closed(handle, "the delivery did not close after a later check found the merge")
            assert status == WorkflowExecutionStatus.COMPLETED
            assert len(checks.calls) >= 2


class TestMergeOrCloseReportedBySignal:
    async def test_merge_signal_completes_the_waiting_delivery_at_once(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        checks = PullRequestChecks(always(OPEN))
        async with waiting_delivery(skipping_env, delivery_state(number=7), checks) as handle:
            await wait_started(handle)
            await handle.signal(DeliveryWorkflow.pull_request_closed, args=[7, True])
            status = await wait_closed(handle, "the delivery did not stop waiting after the merge signal")
            assert status == WorkflowExecutionStatus.COMPLETED
            assert await lifetime(handle) < PULL_REQUEST_CHECK_INTERVAL

    async def test_close_signal_without_merge_cancels_the_waiting_delivery(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        checks = PullRequestChecks(always(OPEN))
        async with waiting_delivery(skipping_env, delivery_state(number=7), checks) as handle:
            await wait_started(handle)
            await handle.signal(DeliveryWorkflow.pull_request_closed, args=[7, False])
            status = await wait_closed(handle, "the delivery did not stop waiting after the close signal")
            assert status == WorkflowExecutionStatus.CANCELED
            assert await lifetime(handle) < PULL_REQUEST_CHECK_INTERVAL

    async def test_signal_about_another_pull_request_changes_nothing(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        checks = PullRequestChecks(always(OPEN))
        async with waiting_delivery(skipping_env, delivery_state(number=7), checks) as handle:
            await wait_started(handle)
            await handle.signal(DeliveryWorkflow.pull_request_closed, args=[99, True])
            await asyncio.sleep(1)
            assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING

            await handle.signal(DeliveryWorkflow.pull_request_closed, args=[7, True])
            status = await wait_closed(handle, "the delivery did not stop waiting after the signal for its pull request")
            assert status == WorkflowExecutionStatus.COMPLETED
            assert await lifetime(handle) < PULL_REQUEST_CHECK_INTERVAL


class TestDeliveryAlreadyWaiting:
    async def test_delivery_resumed_from_its_waiting_state_completes_on_merge(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        """A run started from the state an earlier run waited with (record_only) closes too."""
        state = delivery_state("sess-old", number=12, record_only=True)
        checks = PullRequestChecks(always(MERGED))
        async with waiting_delivery(skipping_env, state, checks) as handle:
            status = await wait_closed(handle, "the resumed delivery did not complete after the merge")
            assert status == WorkflowExecutionStatus.COMPLETED
            assert checks.calls[:1] == [("acme/widgets", 12)]
            assert await lifetime(handle) <= PULL_REQUEST_CHECK_INTERVAL + SLACK

    async def test_delivery_resumed_from_its_waiting_state_is_cancelled_on_close(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        state = delivery_state("sess-old", number=12, record_only=True)
        checks = PullRequestChecks(always(OPEN))
        async with waiting_delivery(skipping_env, state, checks) as handle:
            await wait_started(handle)
            await handle.signal(DeliveryWorkflow.pull_request_closed, args=[12, False])
            status = await wait_closed(handle, "the resumed delivery did not end cancelled after the close")
            assert status == WorkflowExecutionStatus.CANCELED


class TestOnboardingDelivery:
    async def test_onboarding_completes_when_its_pull_request_is_merged(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        state = delivery_state("onb-1", number=3, onboarding=True)
        checks = PullRequestChecks(always(MERGED))
        async with waiting_delivery(skipping_env, state, checks) as handle:
            status = await wait_closed(handle, "the onboarding did not complete after the merge")
            assert status == WorkflowExecutionStatus.COMPLETED
            assert checks.calls[:1] == [("acme/widgets", 3)]
            assert await lifetime(handle) <= PULL_REQUEST_CHECK_INTERVAL + SLACK

    async def test_onboarding_is_cancelled_when_its_pull_request_is_closed(
        self, local_env: WorkflowEnvironment,
    ) -> None:
        # No time is skipped here. The time-skipping server can skip past its default
        # execution timeout while the run's request to cancel itself is still pending,
        # so the run times out instead of ending cancelled.
        state = delivery_state("onb-1", number=3, onboarding=True)
        checks = PullRequestChecks(always(OPEN))
        async with waiting_delivery(local_env, state, checks) as handle:
            await wait_started(handle)
            await handle.signal(DeliveryWorkflow.pull_request_closed, args=[3, False])
            status = await wait_closed(handle, "the onboarding did not end cancelled after the close")
            assert status == WorkflowExecutionStatus.CANCELED


class TestOpenReviewFindingsStayRaised:
    async def test_merge_completes_the_delivery_and_the_flag_stays_raised(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        state = delivery_state("sess-find", number=5, open_findings=True)
        checks = PullRequestChecks(always(MERGED))
        async with waiting_delivery(skipping_env, state, checks) as handle:
            status = await wait_closed(handle, "the delivery with open findings did not complete after the merge")
            assert status == WorkflowExecutionStatus.COMPLETED
            assert checks.calls[:1] == [("acme/widgets", 5)]
            assert await lifetime(handle) <= PULL_REQUEST_CHECK_INTERVAL + SLACK
            reported = await handle.query(DeliveryWorkflow.get_state)
            assert reported.progress.open_findings is True

    async def test_close_cancels_the_delivery_and_the_flag_stays_raised(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        state = delivery_state("sess-find", number=5, open_findings=True)
        checks = PullRequestChecks(always(OPEN))
        async with waiting_delivery(skipping_env, state, checks) as handle:
            await wait_started(handle)
            await handle.signal(DeliveryWorkflow.pull_request_closed, args=[5, False])
            status = await wait_closed(handle, "the delivery with open findings did not end cancelled after the close")
            assert status == WorkflowExecutionStatus.CANCELED
            assert await lifetime(handle) < PULL_REQUEST_CHECK_INTERVAL
            reported = await handle.query(DeliveryWorkflow.get_state)
            assert reported.progress.open_findings is True

    async def test_onboarding_with_open_findings_completes_on_merge(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        state = delivery_state("onb-find", number=9, onboarding=True, open_findings=True)
        checks = PullRequestChecks(always(MERGED))
        async with waiting_delivery(skipping_env, state, checks) as handle:
            status = await wait_closed(handle, "the onboarding with open findings did not complete after the merge")
            assert status == WorkflowExecutionStatus.COMPLETED
            assert checks.calls[:1] == [("acme/widgets", 9)]
            assert await lifetime(handle) <= PULL_REQUEST_CHECK_INTERVAL + SLACK
            reported = await handle.query(DeliveryWorkflow.get_state)
            assert reported.progress.open_findings is True


class TestPushWithoutPullRequest:
    async def test_architecture_only_onboarding_completes_right_after_a_push_without_pull_request(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        opened: list[str] = []

        @activity.defn(name="publish_onboarding_architecture")
        async def publish_onboarding_architecture(
            session_id: str, repository: Any, architecture: str, structure: dict[str, Any] | None,
        ) -> Any:
            return {
                "branch": "onboarding/architecture", "commit": "abc123",
                "url": "https://github.com/acme/widgets/tree/onboarding/architecture",
                "verified": False, "ignored": [], "ignored_count": 0,
                "findings": [], "findings_count": 0,
            }

        @activity.defn(name="write_pull_request_text")
        async def write_pull_request_text(session_id: str, publication: Any, change: dict[str, Any]) -> Any:
            return {"title": "Add the architecture", "summary": "Adds contracts/architecture.md."}

        @activity.defn(name="open_pull_request")
        async def open_pull_request(
            session_id: str, repository: Any, publication: Any, change: dict[str, Any],
            text: Any | None = None,
        ) -> Any:
            opened.append(session_id)
            raise ApplicationError(
                "the pull_requests permission is missing", type="PullRequestError", non_retryable=True,
            )

        checks = PullRequestChecks(always(OPEN))
        async with waiting_delivery(
            skipping_env, architecture_only_state(), checks,
            extra=(publish_onboarding_architecture, write_pull_request_text, open_pull_request),
        ) as handle:
            status = await wait_closed(handle, "the delivery kept waiting after a push that has no pull request")
            assert status == WorkflowExecutionStatus.COMPLETED
            assert await lifetime(handle) < PULL_REQUEST_CHECK_INTERVAL
            progress = await handle.result()
            assert progress.publication_status == "completed"
            assert progress.pull_request_status == "error"
            assert progress.pull_request is None
            assert opened == ["onb-1"]
            assert checks.calls == []


class TestCheckIntervalDefault:
    async def test_the_check_interval_defaults_to_one_hour(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        assert PULL_REQUEST_CHECK_INTERVAL == timedelta(hours=1)
        checks = PullRequestChecks(always(OPEN))
        async with waiting_delivery(skipping_env, delivery_state(), checks) as handle:
            await wait_started(handle)
            await skipping_env.sleep(timedelta(minutes=50))
            assert checks.calls == [], "the pull request must not be checked before one hour has passed"
            await skipping_env.sleep(timedelta(minutes=20))
            await wait_until(
                lambda: len(checks.calls) >= 1,
                "the waiting delivery did not check its pull request after one hour",
            )
            assert checks.calls[0] == ("acme/widgets", 7)
