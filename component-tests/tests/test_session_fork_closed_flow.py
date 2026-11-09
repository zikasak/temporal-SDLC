"""Forking a session of a flow that has completed or been cancelled answers 409.

Seams: app.requirements.service.fork_session and app.architecture.service.fork_session, with
their Temporal client pointed at a local Temporal dev server that runs fakes of the session
workflows they reach by name.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

import pytest
import pytest_asyncio
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.testing import WorkflowEnvironment

import app.architecture.service as architecture_service
import app.requirements.service as requirements_service
import app.temporal.registry as registry
import app.temporal.sessions as sessions
from app.temporal.sessions import SessionBusy
from helpers import (
    FakeArchitecture, FakeRequirements, architecture_state, bounded, close_workflow,
    connect_to, count_executions, make_worker, patch_everywhere, requirements_state,
    unique_queue, wait_started,
)

CLOSED = pytest.mark.parametrize("outcome", ["completed", "canceled"])
CLOSED_STATUS = {"completed": WorkflowExecutionStatus.COMPLETED, "canceled": WorkflowExecutionStatus.CANCELED}


@dataclass
class Harness:
    client: Client
    queue: str

    async def start_requirements(self, session_id: str) -> WorkflowHandle:
        handle = await self.client.start_workflow(
            FakeRequirements.run, requirements_state(), id=session_id, task_queue=self.queue,
        )
        await wait_started(handle)
        return handle

    async def start_architecture(self, session_id: str, source_session_id: str) -> WorkflowHandle:
        handle = await self.client.start_workflow(
            FakeArchitecture.run, architecture_state(source_session_id), id=session_id, task_queue=self.queue,
        )
        await wait_started(handle)
        return handle


@pytest_asyncio.fixture
async def harness(local_env: WorkflowEnvironment, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    async def register(workflow_id: str) -> None:
        return None

    modules = [requirements_service, architecture_service, sessions]
    patch_everywhere(monkeypatch, modules, "connect_temporal", connect_to(local_env.client))
    patch_everywhere(monkeypatch, [registry, requirements_service, architecture_service], "register", register)

    queue = unique_queue()
    async with make_worker(local_env.client, queue, [FakeRequirements, FakeArchitecture]):
        yield Harness(local_env.client, queue)


class TestForkRequirementsSession:
    @CLOSED
    async def test_fork_is_allowed_while_the_flow_runs_and_refused_after_it_closed(
        self, harness: Harness, outcome: str,
    ) -> None:
        await harness.start_requirements("req-open")
        closed = await harness.start_requirements("req-closed")
        await close_workflow(closed, outcome)

        forked_id = await bounded(
            requirements_service.fork_session("req-open", 0),
            "a session of a running flow could not be forked",
        )
        assert forked_id and forked_id != "req-open"
        forked = await harness.client.get_workflow_handle(forked_id).describe()
        assert forked.workflow_type == "RequirementsWorkflow"
        assert forked.status == WorkflowExecutionStatus.RUNNING

        with pytest.raises(SessionBusy):
            await bounded(
                requirements_service.fork_session("req-closed", 0),
                "the fork of a closed flow's session was neither refused nor answered",
            )

        assert (await closed.describe()).status == CLOSED_STATUS[outcome]
        assert await count_executions(harness.client, "RequirementsWorkflow", 3) == 3, (
            "the refused fork must not start a session"
        )


class TestForkArchitectureSession:
    @CLOSED
    async def test_fork_of_an_architecture_session_of_a_closed_flow_is_refused(
        self, harness: Harness, outcome: str,
    ) -> None:
        root = await harness.start_requirements("req-1")
        await harness.start_architecture("arch-1", "req-1")
        await close_workflow(root, outcome)

        with pytest.raises(SessionBusy):
            await bounded(
                architecture_service.fork_session("arch-1", 0),
                "the fork of an architecture session of a closed flow was neither refused nor answered",
            )

        assert await count_executions(harness.client, "ArchitectureWorkflow", 1) == 1, (
            "the refused fork must not start a session"
        )
