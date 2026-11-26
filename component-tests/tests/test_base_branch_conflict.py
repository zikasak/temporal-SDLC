"""A waiting pull request that conflicts with its base branch gets a base-merge fix round.

Seam: app.delivery.workflows.DeliveryWorkflow, run in-process on a time-skipping Temporal
test server with fakes of the activities it calls by their registered names. The delivery
driven here is an architecture-only onboarding's (it runs no agent, verification or review),
the one kind of round whose activities the stubs name completely.
"""
from __future__ import annotations

import asyncio
import dataclasses
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator, Callable

import pytest
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus, WorkflowHandle
from temporalio.testing import WorkflowEnvironment

from app.delivery.models import PULL_REQUEST_CHECK_INTERVAL, DeliveryState, delivery_id
from app.delivery.workflows import DeliveryWorkflow
from helpers import bounded, delivery_state, make_worker, unique_queue, wait_started, wait_until

SESSION = "onb-base"
NUMBER = 7
BASE_TIP = "b" * 40
FILES = ["README.md", "contracts/architecture.md"]
SLACK = timedelta(minutes=1)


def waiting_onboarding() -> DeliveryState:
    """An architecture-only onboarding's delivery that waits after its push, with pull request 7."""
    state = delivery_state(SESSION, number=NUMBER, onboarding=True)
    state.spec = dataclasses.replace(
        state.spec, kind=None, requirements=[], requirements_document="",
        architecture="# Architecture\n\nOne module.\n",
        architecture_structure={"components": [{"name": "core"}]},
        architecture_only=True,
    )
    return state


class GitHub:
    """Fakes of the activities a waiting delivery calls about its pull request, recording the calls."""

    def __init__(self, conflicting: Callable[["GitHub"], bool]) -> None:
        self._conflicting = conflicting
        self.state_calls: list[tuple[str, int]] = []
        self.conflict_calls: list[tuple[str, int]] = []
        self.comments: list[tuple[str, int, str]] = []
        self.merges: list[tuple[str, Any, str, list[str]]] = []
        self.merge_gate: asyncio.Event | None = None
        self.merge_started = asyncio.Event()

    def activities(self) -> list[Callable[..., Any]]:
        gh = self

        @activity.defn(name="pull_request_state")
        async def pull_request_state(full_name: str, number: int) -> dict[str, Any]:
            gh.state_calls.append((full_name, number))
            return {"state": "open", "merged": False}

        @activity.defn(name="pull_request_conflicts")
        async def pull_request_conflicts(full_name: str, number: int) -> dict[str, Any]:
            gh.conflict_calls.append((full_name, number))
            if gh._conflicting(gh):
                return {
                    "conflicting": True, "base": "main", "base_commit": BASE_TIP,
                    "files": list(FILES), "changes": "the base branch edited both files",
                }
            return {"conflicting": False, "base": "main", "base_commit": BASE_TIP,
                    "files": [], "changes": ""}

        @activity.defn(name="comment_pull_request")
        async def comment_pull_request(full_name: str, number: int, text: str) -> str:
            gh.comments.append((full_name, number, text))
            return f"https://github.com/{full_name}/pull/{number}#issuecomment-{len(gh.comments)}"

        @activity.defn(name="publish_base_merge")
        async def publish_base_merge(
            session_id: str, repository: Any, base_commit: str, files: list[str],
        ) -> Any:
            gh.merges.append((session_id, repository, base_commit, list(files)))
            gh.merge_started.set()
            if gh.merge_gate is not None:
                await gh.merge_gate.wait()
            return {
                "branch": f"delivery/{SESSION}", "commit": "c" * 40,
                "url": f"https://github.com/acme/widgets/tree/delivery/{SESSION}",
                "verified": False, "ignored": [], "ignored_count": 0,
            }

        return [pull_request_state, pull_request_conflicts, comment_pull_request, publish_base_merge]

    def texts(self) -> list[str]:
        return [text for _, _, text in self.comments]


def conflicting_until_merged(gh: GitHub) -> bool:
    return not gh.merges


def always_conflicting(gh: GitHub) -> bool:
    return True


@asynccontextmanager
async def waiting(
    env: WorkflowEnvironment, gh: GitHub, state: DeliveryState | None = None,
) -> AsyncIterator[WorkflowHandle[Any, Any]]:
    state = state if state is not None else waiting_onboarding()
    queue = unique_queue()
    worker = make_worker(env.client, queue, [DeliveryWorkflow], gh.activities())
    async with worker:
        handle = await env.client.start_workflow(
            DeliveryWorkflow.run, state, id=delivery_id(SESSION), task_queue=queue,
        )
        try:
            await wait_started(handle)
            yield handle
        finally:
            try:
                await handle.terminate("test finished")
            except Exception:
                pass


async def advance_until(
    env: WorkflowEnvironment, condition: Callable[[], bool], what: str, rounds: int = 10,
) -> None:
    """Let check intervals pass (time skipping) until `condition` holds; fail saying `what`."""
    for _ in range(rounds):
        if condition():
            return
        await env.sleep(PULL_REQUEST_CHECK_INTERVAL + SLACK)
        await asyncio.sleep(0.3)
    if not condition():
        pytest.fail(f"timed out: {what}")


async def wait_state(
    handle: WorkflowHandle[Any, Any], condition: Callable[[DeliveryState], bool], what: str,
    timeout: float = 5.0,
) -> DeliveryState:
    """Poll the get_state query (bounded) until `condition` holds of it; fail saying `what`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        state = await handle.query(DeliveryWorkflow.get_state)
        if condition(state):
            return state
        if loop.time() > deadline:
            pytest.fail(f"timed out: {what}")
        await asyncio.sleep(0.05)


class TestConflictFoundBySignal:
    async def test_waiting_delivery_starts_a_round_on_a_conflict(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        # requirement: FR-214, FR-215, FR-216, FR-218, FR-220
        # A waiting api_change delivery: only what comes before its agent stage is observed
        # (the agent, verification and review activities are not stubbed).
        gh = GitHub(conflicting_until_merged)
        state = delivery_state(SESSION, number=NUMBER, language="python")
        async with waiting(skipping_env, gh, state) as handle:
            await handle.signal("pull_request_conflicted", NUMBER)
            await wait_until(lambda: len(gh.comments) >= 1,
                             "no start comment was posted for the conflicting pull request")
            assert gh.conflict_calls[0] == ("acme/widgets", NUMBER)
            full_name, number, start = gh.comments[0]
            assert (full_name, number) == ("acme/widgets", NUMBER)
            for path in FILES:
                assert path in start
            assert "round 1 of 2" in start.lower()
            running = await wait_state(
                handle, lambda s: s.merge_fix is not None,
                "the progress state did not show the base merge round",
            )
            assert running.merge_fix is not None
            assert running.merge_fix["commit"] == BASE_TIP
            assert running.merge_fix["files"] == FILES
            assert running.merge_fix["round"] == 1


    async def test_signal_runs_a_round_that_pushes_the_merge_of_the_base_tip(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        # requirement: FR-214, FR-215, FR-216, FR-219, FR-220
        gh = GitHub(conflicting_until_merged)
        async with waiting(skipping_env, gh) as handle:
            await handle.signal("pull_request_conflicted", NUMBER)
            await wait_until(lambda: len(gh.merges) >= 1 and len(gh.comments) >= 2,
                             "no base merge round ran after the pull_request_conflicted signal")
            assert gh.conflict_calls[0] == ("acme/widgets", NUMBER)
            session_id, repository, base_commit, files = gh.merges[0]
            assert session_id == SESSION
            assert repository["full_name"] == "acme/widgets"
            assert base_commit == BASE_TIP
            assert files == FILES
            assert len(gh.merges) == 1

    async def test_signal_about_another_pull_request_changes_nothing(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        # requirement: FR-216
        gh = GitHub(conflicting_until_merged)
        async with waiting(skipping_env, gh) as handle:
            await handle.signal("pull_request_conflicted", 99)
            await asyncio.sleep(1)
            assert gh.conflict_calls == []
            await handle.signal("pull_request_conflicted", NUMBER)
            await wait_until(lambda: len(gh.conflict_calls) >= 1,
                             "the signal for the delivery's own pull request did not check its conflicts")
            assert set(gh.conflict_calls) == {("acme/widgets", NUMBER)}
            await wait_until(lambda: len(gh.merges) >= 1, "no base merge round ran")


class TestConflictFoundByThePeriodicCheck:
    async def test_check_interval_finds_the_conflict_and_runs_the_round(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        # requirement: FR-214, FR-216, FR-219
        gh = GitHub(conflicting_until_merged)
        async with waiting(skipping_env, gh) as handle:
            await advance_until(skipping_env, lambda: len(gh.merges) >= 1,
                                "the periodic check did not start a base merge round")
            assert gh.state_calls, "the check must read the pull request's state first"
            assert gh.conflict_calls[0] == ("acme/widgets", NUMBER)
            assert gh.merges[0][2] == BASE_TIP
            assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING

    async def test_signal_and_check_together_start_a_single_round(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        # requirement: FR-216
        gh = GitHub(conflicting_until_merged)
        gh.merge_gate = asyncio.Event()
        async with waiting(skipping_env, gh) as handle:
            await handle.signal("pull_request_conflicted", NUMBER)
            await bounded(gh.merge_started.wait(), "the round did not reach its push")
            await handle.signal("pull_request_conflicted", NUMBER)
            await handle.signal("pull_request_conflicted", NUMBER)
            # No workflow time passes while the push is held (it could time the attempt out).
            await asyncio.sleep(0.5)
            starts = [t for t in gh.texts() if "round" in t.lower()]
            assert len(starts) == 1 and "round 1 of 2" in starts[0].lower(), (
                "a round already running must not be started again")
            gh.merge_gate.set()
            await wait_until(lambda: len(gh.comments) >= 2, "the round did not comment its outcome")
            # The round has ended and the pull request no longer conflicts: later checks
            # and signals start no further round.
            await handle.signal("pull_request_conflicted", NUMBER)
            for _ in range(2):
                await skipping_env.sleep(PULL_REQUEST_CHECK_INTERVAL + SLACK)
                await asyncio.sleep(0.3)
            assert not any("round 2" in t.lower() for t in gh.texts())
            assert len(gh.comments) == 2


class TestRoundReporting:
    async def test_round_is_commented_in_the_pull_request_and_shown_in_the_progress(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        # requirement: FR-218, FR-215
        gh = GitHub(conflicting_until_merged)
        gh.merge_gate = asyncio.Event()
        async with waiting(skipping_env, gh) as handle:
            await handle.signal("pull_request_conflicted", NUMBER)
            await bounded(gh.merge_started.wait(), "the round did not reach its push")

            # While the round runs: its start comment was posted and the state shows the round.
            assert len(gh.comments) == 1
            full_name, number, start = gh.comments[0]
            assert (full_name, number) == ("acme/widgets", NUMBER)
            for path in FILES:
                assert path in start
            assert "round 1 of 2" in start.lower()
            running = await handle.query(DeliveryWorkflow.get_state)
            assert running.merge_fix is not None
            assert running.merge_fix["commit"] == BASE_TIP
            assert running.merge_fix["files"] == FILES
            assert running.merge_fix["round"] == 1

            gh.merge_gate.set()
            await wait_until(lambda: len(gh.comments) >= 2, "no comment reported the outcome of the round")
            full_name, number, outcome = gh.comments[1]
            assert (full_name, number) == ("acme/widgets", NUMBER)
            assert outcome != start
            # The outcome comment identifies the merge commit that was pushed.
            assert "ccccccc" in outcome
            assert len(gh.comments) == 2

            # The progress shows the outcome: the pushed merge is the delivery's publication
            # and the round is over.
            ended = await wait_state(
                handle, lambda s: s.merge_fix is None,
                "the progress state still showed the base merge round after it ended",
            )
            assert ended.progress.publication is not None
            assert ended.progress.publication["commit"] == "c" * 40


class TestRoundLimit:
    async def test_two_rounds_then_one_comment_that_the_conflict_remains(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        # requirement: FR-217, FR-218
        gh = GitHub(always_conflicting)
        async with waiting(skipping_env, gh) as handle:
            await handle.signal("pull_request_conflicted", NUMBER)
            # Comments in order: start 1, outcome 1, start 2, outcome 2, then the one comment
            # that the conflict remains.
            await advance_until(
                skipping_env, lambda: len(gh.comments) >= 5,
                "two rounds did not run and then comment once more that the conflict remains",
            )
            await handle.signal("pull_request_conflicted", NUMBER)
            for _ in range(3):
                await skipping_env.sleep(PULL_REQUEST_CHECK_INTERVAL + SLACK)
                await asyncio.sleep(0.3)

            assert len(gh.merges) == 2, "no third automatic round may start"
            texts = gh.texts()
            assert len(texts) == 5, "after the limit comment nothing more may be commented"
            assert "round 1 of 2" in texts[0].lower()
            assert "round 2 of 2" in texts[2].lower()
            assert not any("round 3" in t.lower() for t in texts)
            for path in FILES:
                assert path in texts[0] and path in texts[2]
            assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING


class TestAcceptance:
    async def test_conflicting_pull_request_becomes_mergeable_through_a_pushed_merge(
        self, skipping_env: WorkflowEnvironment,
    ) -> None:
        # requirement: FR-220, FR-219
        gh = GitHub(conflicting_until_merged)
        async with waiting(skipping_env, gh) as handle:
            await handle.signal("pull_request_conflicted", NUMBER)
            await wait_until(lambda: len(gh.merges) == 1 and len(gh.comments) >= 2,
                             "the round did not push the merge of the base branch")
            assert gh.merges[0][2] == BASE_TIP
            # GitHub now reports the pull request mergeable: later checks start no round.
            for _ in range(2):
                await skipping_env.sleep(PULL_REQUEST_CHECK_INTERVAL + SLACK)
                await asyncio.sleep(0.3)
            assert len(gh.merges) == 1
            assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
