"""Mentions and decisions on a delivery whose flow has closed (its pull request was merged or
closed) start a new top-level flow; while the delivery is open they go to that delivery.

Seam: app.delivery.service, with its Temporal client pointed at a local Temporal dev server
that runs a fake "DeliveryWorkflow", and fakes of the mention history, the mention
recording and the session registry.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator

import pytest
import pytest_asyncio
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.testing import WorkflowEnvironment

import app.delivery.mentions as mentions
import app.delivery.service as service
import app.temporal.registry as registry
import app.temporal.sessions as sessions
from app.delivery.models import DeliveryState, delivery_id
from helpers import (
    RECORD, FakeDelivery, bounded, close_workflow, connect_to, deliveries_besides, delivery_state,
    in_background, make_worker, mention_for, patch_everywhere, started_with, unique_queue,
    wait_started, wait_until,
)

INSTRUCTIONS = "Rename the endpoint to /widgets and update the tests."


@dataclass
class Harness:
    client: Client
    queue: str
    registered: list[str]

    async def start_delivery(self, state: DeliveryState) -> WorkflowHandle:
        handle = await self.client.start_workflow(
            FakeDelivery.run, state, id=delivery_id(state.spec.session_id), task_queue=self.queue,
        )
        await wait_started(handle)
        return handle


@pytest_asyncio.fixture
async def harness(local_env: WorkflowEnvironment, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    registered: list[str] = []

    async def recording(session_id: str, comment_type: str, comment_id: int) -> str | None:
        return RECORD.recorded.get((session_id, comment_type, comment_id))

    async def history(session_id: str) -> list[dict]:
        return []

    async def register(workflow_id: str) -> None:
        registered.append(workflow_id)

    patch_everywhere(monkeypatch, [service, sessions], "connect_temporal", connect_to(local_env.client))
    patch_everywhere(monkeypatch, [mentions, service], "recording", recording)
    patch_everywhere(monkeypatch, [mentions, service], "history", history)
    patch_everywhere(monkeypatch, [registry, service], "register", register)

    queue = unique_queue()
    async with make_worker(local_env.client, queue, [FakeDelivery]):
        yield Harness(local_env.client, queue, registered)


def other_flows(own_session: str) -> list:
    """The deliveries the service started besides the one of `own_session`."""
    return [run for run in RECORD.delivery_runs if run.workflow_id != delivery_id(own_session)]


CLOSED = pytest.mark.parametrize("outcome", ["completed", "canceled"])
CLOSED_STATUS = {"completed": WorkflowExecutionStatus.COMPLETED, "canceled": WorkflowExecutionStatus.CANCELED}


class TestMentionOnAPullRequest:
    @CLOSED
    async def test_mention_on_a_closed_flow_goes_to_a_new_top_level_flow(
        self, harness: Harness, outcome: str,
    ) -> None:
        original = delivery_state("sess-closed", number=7, language="python")
        closed = await harness.start_delivery(original)
        await close_workflow(closed, outcome)

        # The new flow's task queue is not stated: look for it on the server, not in the fake.
        async with in_background(service.receive_mention("sess-closed", mention_for(101))) as call:
            started = await deliveries_besides(
                harness.client, [delivery_id("sess-closed")],
                "the mention of a closed flow's pull request did not start a new delivery",
            )
            assert len(started) == 1, "the mention must start exactly one new delivery"
            new = started[0]
            assert new.id != delivery_id("sess-closed")
            assert new.parent_id is None, "the new flow must be top-level"
            state = await started_with(harness.client, new.id)
            assert state.spec.session_id not in ("", "sess-closed")
            assert state.spec.requirements_document == original.spec.requirements_document
            assert state.spec.repository is not None
            assert state.spec.repository["full_name"] == "acme/widgets"
            assert (await closed.describe()).status == CLOSED_STATUS[outcome], (
                "the closed delivery must stay closed"
            )
            # When the new flow runs on the fake's queue the mention is recorded by it.
            await asyncio.wait({call}, timeout=3)
            if call.done():
                assert call.result() is True
                assert [(m.workflow_id, m.state["comment_id"]) for m in RECORD.mentions] == [(new.id, 101)]

    async def test_mention_goes_to_the_open_delivery_and_only_a_closed_one_gets_a_new_flow(
        self, harness: Harness,
    ) -> None:
        await harness.start_delivery(delivery_state("sess-open", number=7))
        closed = await harness.start_delivery(delivery_state("sess-closed", number=8))
        await close_workflow(closed, "completed")

        assert await bounded(
            service.receive_mention("sess-open", mention_for(201, number=7)),
            "the mention of an open pull request was not handled",
        ) is True
        assert [(m.workflow_id, m.state["comment_id"]) for m in RECORD.mentions] == [
            (delivery_id("sess-open"), 201),
        ]
        known = [delivery_id("sess-open"), delivery_id("sess-closed")]
        listed = [w.id async for w in harness.client.list_workflows('WorkflowType = "DeliveryWorkflow"')]
        assert set(listed) <= set(known), "a mention on an open pull request must not start another flow"

        async with in_background(service.receive_mention("sess-closed", mention_for(202, number=8))) as call:
            started = await deliveries_besides(
                harness.client, known,
                "the mention of a closed flow's pull request did not start a new delivery",
            )
            assert len(started) == 1
            new_flow = started[0]
            assert new_flow.parent_id is None
            await asyncio.wait({call}, timeout=3)
            if call.done():
                assert call.result() is True
                assert RECORD.mentions[-1].workflow_id == new_flow.id
                assert RECORD.mentions[-1].state["comment_id"] == 202


class TestOpenReviewFindingsDecisions:
    @CLOSED
    async def test_accepting_findings_of_a_closed_flow_clears_the_flag(
        self, harness: Harness, outcome: str,
    ) -> None:
        closed = await harness.start_delivery(delivery_state("sess-closed", open_findings=True))
        await close_workflow(closed, outcome)

        progress = await bounded(
            service.accept_findings("sess-closed", "alice"),
            "the findings of a closed flow could not be accepted",
        )

        assert progress.open_findings is False
        assert progress.findings_acceptance is not None
        assert progress.findings_acceptance["accepted_by"] == "alice"
        assert progress.findings_acceptance["accepted_at"]
        assert (await closed.describe()).status == CLOSED_STATUS[outcome], (
            "accepting findings must not reopen the closed delivery: nothing runs"
        )
        assert [r.workflow_id for r in RECORD.delivery_runs] == [delivery_id("sess-closed")], (
            "accepting findings must not start or restart any delivery run"
        )

        reported = await bounded(service.get_progress("sess-closed"), "the delivery reported no progress")
        assert reported is not None
        assert reported.open_findings is False
        assert reported.findings_acceptance is not None
        assert reported.findings_acceptance["accepted_by"] == "alice"
        assert other_flows("sess-closed") == [], "accepting findings must not start a new flow"
        assert all(run.state.instructions is None for run in RECORD.delivery_runs)

    @CLOSED
    async def test_instructions_for_a_closed_flow_start_a_new_top_level_flow(
        self, harness: Harness, outcome: str,
    ) -> None:
        original = delivery_state("sess-closed", open_findings=True, language="python")
        closed = await harness.start_delivery(original)
        await close_workflow(closed, outcome)

        # The new flow's task queue is not stated: look for it on the server, not in the fake.
        async with in_background(service.instruct_findings("sess-closed", INSTRUCTIONS)) as call:
            started = await deliveries_besides(
                harness.client, [delivery_id("sess-closed")],
                "the instructions did not start a new delivery",
            )
            assert len(started) == 1, "the instructions must start exactly one new delivery"
            new = started[0]
            assert new.id != delivery_id("sess-closed")
            assert new.parent_id is None, "the new flow must be top-level"
            state = await started_with(harness.client, new.id)
            assert state.instructions == INSTRUCTIONS
            assert state.spec.session_id not in ("", "sess-closed"), "the new flow needs a session of its own"
            assert state.spec.requirements_document == original.spec.requirements_document
            assert state.spec.repository is not None
            assert state.spec.repository["full_name"] == "acme/widgets"
            assert state.spec.language == "python"
            assert RECORD.restarts == [], "the closed delivery must not be restarted"
            assert (await closed.describe()).status == CLOSED_STATUS[outcome], (
                "the closed delivery must stay closed"
            )
            await asyncio.wait({call}, timeout=3)
            if call.done():
                call.result()

    async def test_instructions_restart_the_open_delivery_on_its_own_session_only_while_it_is_open(
        self, harness: Harness,
    ) -> None:
        await harness.start_delivery(delivery_state("sess-open", open_findings=True, language="python"))
        closed = await harness.start_delivery(delivery_state("sess-closed", open_findings=True, language="python"))
        await close_workflow(closed, "completed")

        await bounded(
            service.instruct_findings("sess-open", INSTRUCTIONS),
            "the instructions about the findings of an open delivery were not handled",
        )

        await wait_until(lambda: len(RECORD.restarts) >= 1, "the open delivery did not get the restart update")
        assert [(r.workflow_id, r.state.instructions, r.state.spec.session_id) for r in RECORD.restarts] == [
            (delivery_id("sess-open"), INSTRUCTIONS, "sess-open"),
        ]
        known = [delivery_id("sess-open"), delivery_id("sess-closed")]
        listed = [w.id async for w in harness.client.list_workflows('WorkflowType = "DeliveryWorkflow"')]
        assert set(listed) <= set(known), "instructions about an open delivery must not start another flow"

        async with in_background(service.instruct_findings("sess-closed", INSTRUCTIONS)) as call:
            started = await deliveries_besides(
                harness.client, known, "the instructions did not start a new delivery",
            )
            assert len(started) == 1
            assert started[0].parent_id is None
            await asyncio.wait({call}, timeout=3)
            if call.done():
                call.result()
        assert len(RECORD.restarts) == 1
