import uuid

import pytest
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from app.conversations import service

TASK_QUEUE = "conversation-fork-test"


@workflow.defn(name="LLMRequestWorkflow")
class FakeRequest:
    @workflow.run
    async def run(self, messages: list, parse_document_id: str | None, session_id: str) -> str:
        return f"Answer {len(messages)}"


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def request(env, messages: list, parent: str = "") -> tuple[str, list]:
    request_id = f"llm-request-{uuid.uuid4().hex}"
    handle = await env.client.start_workflow(
        FakeRequest.run, args=[messages, None, "session"], id=request_id,
        task_queue=TASK_QUEUE, memo={"messages": messages, "parent_request_id": parent},
    )
    answer = await handle.result()
    return request_id, [*messages, {"role": "assistant", "content": answer}]


async def test_answered_at_finds_the_request_that_wrote_the_answer(env, monkeypatch):
    async def connect():
        return env.client

    monkeypatch.setattr(service, "connect_temporal", connect)
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[FakeRequest],
                      workflow_runner=UnsandboxedWorkflowRunner()):
        first, messages = await request(env, [{"role": "user", "content": "One"}])
        second, messages = await request(
            env, [*messages, {"role": "user", "content": "Two"}], first
        )
    # Messages: One, Answer 1, Two, Answer 3.
    for request_id, index in [(first, 1), (second, 3)]:
        description = await env.client.get_workflow_handle(request_id).describe()
        assert await service._answered_at(second, index) == description.close_time
    assert await service._answered_at(second, 2) is None
