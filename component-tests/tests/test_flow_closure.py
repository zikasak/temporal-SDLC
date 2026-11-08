"""A requirements or API design session ends together with its flow's delivery.

Seam: app.temporal.workflows (RequirementsWorkflow, ApiDesignWorkflow), run in-process
with fakes of the children and activities they reach by their registered names.
"""
from __future__ import annotations

from typing import Any, Callable

from temporalio import activity, workflow
from temporalio.client import WorkflowExecutionStatus
from temporalio.testing import WorkflowEnvironment

from app.api_design.models import ApiDesignState
from app.architecture.models import ArchitectureResult, ArchitectureState
from app.delivery.models import delivery_id
from app.requirements.models import RequirementsState
from app.temporal.workflows import ApiDesignWorkflow, RequirementsWorkflow
from helpers import (
    RECORD, FakeDelivery, Started, api_design_state, make_worker, requirements_state,
    unique_queue, wait_closed, wait_until,
)


@workflow.defn(name="ArchitectureWorkflow")
class FinalizedArchitecture:
    """An architecture session that is finalized at once and hands on to an API design."""

    @workflow.run
    async def run(self, state: ArchitectureState) -> ArchitectureResult | None:
        return ArchitectureResult(
            api_design_session_id="api-1",
            design=api_design_state(state.source_session_id),
        )


@workflow.defn(name="ApiDesignWorkflow")
class ChildApiDesign:
    """An API design session that ends when told to (or when cancelled)."""

    @workflow.init
    def __init__(self, state: ApiDesignState) -> None:
        self.done = False
        if not workflow.unsafe.is_replaying():
            info = workflow.info()
            RECORD.child_runs.append(Started(info.workflow_id, info.parent, state))

    @workflow.run
    async def run(self, state: ApiDesignState) -> dict[str, Any] | None:
        await workflow.wait_condition(lambda: self.done)
        return state.document

    @workflow.signal
    def finish(self) -> None:
        self.done = True


@activity.defn(name="register_session")
async def register_session(session_id: str) -> None:
    return None


@activity.defn(name="write_requirements_document")
async def write_requirements_document(
    session_id: str, messages: list[Any], requirements: list[Any], retired: list[Any],
) -> dict[str, Any]:
    return {"document_id": "doc-1"}


@activity.defn(name="read_requirements_document")
async def read_requirements_document(session_id: str, document_id: str) -> str:
    return "# Requirements\n\nServe widgets.\n"


REQUIREMENTS_ACTIVITIES: list[Callable[..., Any]] = [register_session, write_requirements_document, read_requirements_document]


class TestRequirementsSessionEndsWithItsFlow:
    async def test_session_completes_at_once_when_the_api_design_completes(
        self, local_env: WorkflowEnvironment,
    ) -> None:
        queue = unique_queue()
        worker = make_worker(
            local_env.client, queue,
            [RequirementsWorkflow, FinalizedArchitecture, ChildApiDesign], REQUIREMENTS_ACTIVITIES,
        )
        async with worker:
            handle = await local_env.client.start_workflow(
                RequirementsWorkflow.run, requirements_state(), id="req-1", task_queue=queue,
            )
            await handle.execute_update("finalize")
            await wait_until(lambda: len(RECORD.child_runs) == 1, "the requirements session did not start its API design")

            child = local_env.client.get_workflow_handle(RECORD.child_runs[0].workflow_id)
            await child.signal("finish")

            status = await wait_closed(handle, "the requirements session stayed open after its flow completed")
            assert status == WorkflowExecutionStatus.COMPLETED
            document = await handle.result()
            assert document is not None
            assert document["document_id"] == "doc-1"

    async def test_session_ends_cancelled_when_the_api_design_was_cancelled(
        self, local_env: WorkflowEnvironment,
    ) -> None:
        queue = unique_queue()
        worker = make_worker(
            local_env.client, queue,
            [RequirementsWorkflow, FinalizedArchitecture, ChildApiDesign], REQUIREMENTS_ACTIVITIES,
        )
        async with worker:
            handle = await local_env.client.start_workflow(
                RequirementsWorkflow.run, requirements_state(), id="req-1", task_queue=queue,
            )
            await handle.execute_update("finalize")
            await wait_until(lambda: len(RECORD.child_runs) == 1, "the requirements session did not start its API design")

            child = local_env.client.get_workflow_handle(RECORD.child_runs[0].workflow_id)
            await child.cancel()

            status = await wait_closed(handle, "the requirements session stayed open after its flow was cancelled")
            assert status == WorkflowExecutionStatus.CANCELED


class TestApiDesignSessionEndsWithItsDelivery:
    async def test_session_ends_cancelled_at_once_when_its_delivery_was_cancelled(
        self, local_env: WorkflowEnvironment,
    ) -> None:
        # Runs on local_env: its delivery child carries the Repository and Branch search
        # attributes, which only that environment registers.
        queue = unique_queue()
        worker = make_worker(local_env.client, queue, [ApiDesignWorkflow, FakeDelivery])
        async with worker:
            handle = await local_env.client.start_workflow(
                ApiDesignWorkflow.run, api_design_state("req-1"), id="api-1", task_queue=queue,
            )
            await wait_until(lambda: len(RECORD.delivery_runs) == 1, "the API design did not start its delivery")
            assert RECORD.delivery_runs[0].workflow_id == delivery_id("api-1")

            delivery = local_env.client.get_workflow_handle(RECORD.delivery_runs[0].workflow_id)
            await delivery.cancel()

            status = await wait_closed(handle, "the API design session stayed open after its delivery was cancelled")
            assert status == WorkflowExecutionStatus.CANCELED
