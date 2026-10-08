import uuid
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.codegen.workflows import CodegenWorkflow

TASK_QUEUE = "codegen-test"


class FakeActivities:
    def __init__(self) -> None:
        self.fail = False
        self.calls: list[tuple[str, str, str | None]] = []

    @activity.defn(name="generate_server_code")
    async def generate_server_code(
        self, session_id: str, document_id: str, language: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((session_id, document_id, language))
        if self.fail:
            raise ApplicationError("bad spec", non_retryable=True)
        return {"path": f"/generated/{session_id}/server", "files": 12}


@pytest.fixture(scope="session")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@pytest.fixture
async def worker(env):
    activities = FakeActivities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[CodegenWorkflow],
        activities=[activities.generate_server_code],
    ):
        yield activities


async def run(env):
    return await env.client.execute_workflow(
        CodegenWorkflow.run, args=["api-design-session-1", "0" * 32, "java"],
        id=f"codegen-{uuid.uuid4().hex}", task_queue=TASK_QUEUE,
    )


async def test_generates_server_code(env, worker):
    assert await run(env) == {
        "server": {"path": "/generated/api-design-session-1/server", "files": 12}
    }
    assert worker.calls == [("api-design-session-1", "0" * 32, "java")]


async def test_generation_failure_fails_workflow(env, worker):
    worker.fail = True
    with pytest.raises(WorkflowFailureError):
        await run(env)
    assert len(worker.calls) == 1
