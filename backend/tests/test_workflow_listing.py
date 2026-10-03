import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from app.api.routes import workflows as workflows_route
from app.temporal import executions

TASK_QUEUE = "listing-test"


@workflow.defn(name="SayHelloWorkflow")
class FakeHello:
    @workflow.run
    async def run(self, name: str) -> str:
        return f"Hello, {name}!"


@workflow.defn(name="RequirementsWorkflow")
class FakeRequirements:
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_local() as env:
        yield env


@pytest.fixture
async def client(env, monkeypatch):
    async def connect():
        return env.client

    monkeypatch.setattr(executions, "connect_temporal", connect)
    monkeypatch.setattr(executions, "PAGE_SIZE", 2)
    return env.client


async def _listed(workflow_type: str | None, expected: int) -> list[dict]:
    # Visibility is eventually consistent; wait for the new executions to show up.
    for _ in range(50):
        page = await executions.list_executions(workflow_type)
        items = page["workflows"]
        while page["next_page_token"]:
            page = await executions.list_executions(workflow_type, page["next_page_token"])
            items += page["workflows"]
        if len(items) >= expected:
            return items
        await asyncio.sleep(0.2)
    return items


async def test_lists_workflows_filtered_by_type(client):
    async with Worker(client, task_queue=TASK_QUEUE, workflows=[FakeHello, FakeRequirements],
                      workflow_runner=UnsandboxedWorkflowRunner()):
        for name in ("a", "b", "c"):
            await client.execute_workflow(
                "SayHelloWorkflow", name, id=f"hello-{uuid.uuid4()}", task_queue=TASK_QUEUE
            )
        running = await client.start_workflow(
            "RequirementsWorkflow", id=f"requirements-session-{uuid.uuid4().hex}",
            task_queue=TASK_QUEUE,
        )

        hello = await _listed("SayHelloWorkflow", 3)
        assert len(hello) == 3
        assert {item["workflow_type"] for item in hello} == {"SayHelloWorkflow"}
        assert {item["status"] for item in hello} == {"completed"}
        assert all(item["close_time"] for item in hello)

        requirements = await _listed("RequirementsWorkflow", 1)
        assert [item["workflow_id"] for item in requirements] == [running.id]
        assert requirements[0]["status"] == "running"
        assert requirements[0]["close_time"] is None

        everything = await _listed(None, 4)
        assert len(everything) == 4
        await running.terminate()


def test_rejects_unknown_type_and_bad_token():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(workflows_route.router)
    http = TestClient(app)
    assert http.get("/workflows", params={"type": "Nope' OR 1=1"}).status_code == 422
    assert http.get("/workflows", params={"page_token": "***"}).status_code == 422
