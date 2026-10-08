import uuid

import pytest
from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from app.conversations import service

TASK_QUEUE = "conversation-fork-test"


@workflow.defn(name="LLMRequestWorkflow")
class FakeRequest:
    @workflow.run
    async def run(self, messages: list, parse_document_id: str | None, session_id: str) -> str:
        if messages[-1]["content"] == "Fail":
            raise ApplicationError("model failed")
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


@pytest.mark.parametrize("linked", [False, True])
async def test_repository_is_preserved_by_replies_forks_and_retries(env, monkeypatch, repository_selection, linked):
    async def connect():
        return env.client

    async def ignore(*args):
        pass

    async def documents(*args):
        return []

    monkeypatch.setattr(service, "connect_temporal", connect)
    monkeypatch.setattr(service, "TASK_QUEUE", TASK_QUEUE)
    for name in ("touch_documents", "touch_generated_documents", "copy_generated_documents", "copy_session_passages"):
        monkeypatch.setattr(service, name, ignore)
    monkeypatch.setattr(service, "list_generated_documents", documents)
    selected = repository_selection if linked else None

    async def completed(request_id):
        await env.client.get_workflow_handle(request_id).result()
        record = await service.get_llm_request(request_id)
        assert record["repository"] == selected
        assert "token" not in str(record["repository"])
        return request_id

    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[FakeRequest],
                      workflow_runner=UnsandboxedWorkflowRunner()):
        first = await completed(await service.start_llm_request("One", repository=selected))
        second = await completed(await service.start_llm_request("Two", first))
        user_fork = await completed(await service.fork_llm_request(second, 0))
        assert (await service.get_llm_request(user_fork))["session_id"] != (await service.get_llm_request(first))["session_id"]
        assistant_fork = await completed(await service.start_llm_request("Branch", second, fork_after=1))
        await completed(await service.fork_llm_request(assistant_fork, 2))
        failed = await service.start_llm_request("Fail", repository=selected)
        with pytest.raises(WorkflowFailureError):
            await env.client.get_workflow_handle(failed).result()
        retry = await service.retry_llm_request(failed)
        with pytest.raises(WorkflowFailureError):
            await env.client.get_workflow_handle(retry).result()
        assert (await service.get_llm_request(retry))["repository"] == selected
        if not linked:
            legacy, _ = await request(env, [{"role": "user", "content": "Legacy"}])
            assert (await service.get_llm_request(legacy))["repository"] is None
            await completed(await service.start_llm_request("New reply", legacy))
