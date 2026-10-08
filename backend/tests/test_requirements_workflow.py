import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.client import WorkflowUpdateFailedError, WorkflowUpdateStage
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from app.requirements import service
from app.requirements.models import RequirementsState
from app.temporal import sessions
from app.temporal.sessions import InvalidForkPoint
from app.temporal.workflows import RequirementsWorkflow

TASK_QUEUE = "requirements-test"
DOCUMENT = {"document_id": "0" * 32, "filename": "requirements.md",
            "content_type": "text/markdown", "size_bytes": 10, "revision": 1}


class FakeActivities:
    def __init__(self) -> None:
        self.release_turn = asyncio.Event()
        self.release_turn.set()
        self.documents: list[str] = []
        self.fail_turn = False

    @activity.defn(name="requirements_turn")
    async def requirements_turn(
        self, messages: list[dict[str, Any]], requirements: list[dict[str, Any]]
    ) -> dict[str, Any]:
        await self.release_turn.wait()
        if self.fail_turn:
            raise ApplicationError("model unavailable", non_retryable=True)
        number = len(requirements) + 1
        return {
            "reply": f"Question {number}?",
            "requirements": [*requirements, {
                "id": f"FR-{number}", "category": "functional",
                "title": messages[-1]["content"], "description": "D", "priority": "must",
            }],
        }

    @activity.defn(name="write_requirements_document")
    async def write_requirements_document(
        self, session_id: str, messages: list[dict[str, Any]],
        requirements: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.documents.append(session_id)
        return DOCUMENT


@pytest.fixture(scope="session")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@pytest.fixture
async def worker(env):
    activities = FakeActivities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[RequirementsWorkflow],
        activities=[activities.requirements_turn, activities.write_requirements_document],
    ):
        yield activities


async def start(env, state: RequirementsState | None = None):
    return await env.client.start_workflow(
        RequirementsWorkflow.run, state,
        id=f"requirements-session-{uuid.uuid4().hex}", task_queue=TASK_QUEUE,
    )


async def wait_for_status(handle, status: str) -> RequirementsState:
    for _ in range(100):
        state = await handle.query(RequirementsWorkflow.get_state)
        if state.status == status:
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(f"status never became {status}")


@pytest.fixture
def temporal(env, monkeypatch):
    async def connect():
        return env.client

    monkeypatch.setattr(service, "connect_temporal", connect)
    monkeypatch.setattr(sessions, "connect_temporal", connect)
    monkeypatch.setattr(service, "TASK_QUEUE", TASK_QUEUE)


async def test_interview_and_finalize(env, worker):
    handle = await start(env)
    state = await handle.query(RequirementsWorkflow.get_state)
    assert state.status == "interviewing" and state.messages[0]["role"] == "assistant"
    assert state.repository is None

    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(RequirementsWorkflow.finalize)

    await handle.execute_update(RequirementsWorkflow.send_message, "  Book rooms  ")
    state: RequirementsState = await handle.query(RequirementsWorkflow.get_state)
    assert [item["role"] for item in state.messages] == ["assistant", "user", "assistant"]
    assert state.messages[1]["content"] == "Book rooms"
    assert state.requirements[0]["id"] == "FR-1"
    assert state.status == "interviewing"

    await handle.execute_update(RequirementsWorkflow.finalize)
    assert await handle.result() == DOCUMENT
    assert worker.documents == [handle.id]
    state = await handle.query(RequirementsWorkflow.get_state)
    assert state.status == "completed" and state.document == DOCUMENT


async def test_rejects_message_while_turn_running(env, worker):
    handle = await start(env)
    worker.release_turn.clear()
    first = await handle.start_update(
        RequirementsWorkflow.send_message, "First",
        wait_for_stage=WorkflowUpdateStage.ACCEPTED,
    )
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(RequirementsWorkflow.send_message, "Second")
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(RequirementsWorkflow.send_message, "   ")
    worker.release_turn.set()
    await first.result()
    state = await handle.query(RequirementsWorkflow.get_state)
    assert [item["content"] for item in state.messages if item["role"] == "user"] == ["First"]
    await handle.terminate()


async def test_failed_turn_keeps_message_and_can_retry(env, worker):
    handle = await start(env)
    worker.fail_turn = True
    await handle.execute_update(RequirementsWorkflow.send_message, "Book rooms")
    state = await handle.query(RequirementsWorkflow.get_state)
    assert [item["content"] for item in state.messages if item["role"] == "user"] == ["Book rooms"]
    assert state.error and state.status == "interviewing"
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(RequirementsWorkflow.send_message, "Another message")
    worker.fail_turn = False
    await handle.execute_update(RequirementsWorkflow.retry_message)
    state = await handle.query(RequirementsWorkflow.get_state)
    assert [item["role"] for item in state.messages] == ["assistant", "user", "assistant"]
    assert state.requirements[0]["title"] == "Book rooms" and state.error is None
    await handle.terminate()


async def test_idle_session_auto_finalizes(env, worker):
    handle = await start(env)
    await handle.execute_update(RequirementsWorkflow.send_message, "Book rooms")
    await env.sleep(timedelta(hours=25))
    assert await handle.result() == DOCUMENT


async def test_idle_session_without_requirements_fails(env, worker):
    handle = await start(env)
    await env.sleep(timedelta(hours=25))
    assert await handle.result() is None
    state = await handle.query(RequirementsWorkflow.get_state)
    assert state.status == "failed"


async def test_session_forked_after_user_message_answers_it(env, worker):
    handle = await start(env, RequirementsState(messages=[
        {"role": "assistant", "content": "Hello"}, {"role": "user", "content": "Book rooms"},
    ]))
    state = await wait_for_status(handle, "interviewing")
    assert [item["role"] for item in state.messages] == ["assistant", "user", "assistant"]
    assert [item["title"] for item in state.requirements] == ["Book rooms"]
    await handle.terminate()


def titles(state: RequirementsState) -> list[str]:
    return [item["title"] for item in state.requirements]


async def test_fork_keeps_requirements_as_they_were_at_the_message(env, worker, temporal, repository_selection):
    source = env.client.get_workflow_handle(await service.start_session(repository=repository_selection))
    assert (await source.query(RequirementsWorkflow.get_state)).repository == repository_selection
    await source.execute_update(RequirementsWorkflow.send_message, "A")
    await source.execute_update(RequirementsWorkflow.send_message, "B")
    # opening, A, Question 1?, B, Question 2?

    fork = env.client.get_workflow_handle(await service.fork_session(source.id, 2))
    state = await fork.query(RequirementsWorkflow.get_state)
    assert [item["content"] for item in state.messages][1:] == ["A", "Question 1?"]
    assert titles(state) == ["A"] and state.status == "interviewing"
    assert state.forked_from == {"session_id": source.id, "message_index": 2}
    assert (await service.get_session(fork.id))["repository"] == repository_selection

    # Forking after a user message answers it again.
    regenerated = env.client.get_workflow_handle(await service.fork_session(source.id, 3))
    state = await wait_for_status(regenerated, "interviewing")
    assert [item["content"] for item in state.messages][3:] == ["B", "Question 2?"]
    assert titles(state) == ["A", "B"]

    # A fork of a fork reaches back into the source for earlier messages.
    await fork.execute_update(RequirementsWorkflow.send_message, "C")
    state = await fork.query(RequirementsWorkflow.get_state)
    assert titles(state) == ["A", "C"]
    for index, expected in [(0, []), (2, ["A"]), (4, ["A", "C"])]:
        nested = env.client.get_workflow_handle(await service.fork_session(fork.id, index))
        nested_state = await nested.query(RequirementsWorkflow.get_state)
        assert titles(nested_state) == expected
        assert nested_state.repository == repository_selection
        await nested.terminate()

    with pytest.raises(InvalidForkPoint):
        await service.fork_session(source.id, 5)
    for handle in (source, fork, regenerated):
        await handle.terminate()


@workflow.defn(name="ContinuingRequirements")
class ContinuingRequirements:
    """Takes one turn per run, continuing as new until it has two answers."""

    @workflow.run
    async def run(self, state: RequirementsState) -> None:
        self.state = state
        state.messages.append({"role": "user", "content": f"Topic {len(state.messages)}"})
        result = await workflow.execute_activity(
            "requirements_turn", args=[state.messages, state.requirements],
            start_to_close_timeout=timedelta(seconds=10),
        )
        state.messages.append({"role": "assistant", "content": result["reply"]})
        state.requirements = result["requirements"]
        if len(state.messages) < 5:
            workflow.continue_as_new(state)

    @workflow.query
    def get_state(self) -> RequirementsState:
        return self.state


async def test_state_at_message_spans_continue_as_new_runs(env, worker, temporal, repository_selection):
    session_id = f"requirements-session-{uuid.uuid4().hex}"
    async with Worker(
        env.client, task_queue="continuing-test", workflows=[ContinuingRequirements],
        activities=[worker.requirements_turn], workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        await env.client.execute_workflow(
            ContinuingRequirements.run,
            RequirementsState(messages=[{"role": "assistant", "content": "Hello"}], repository=repository_selection),
            id=session_id, task_queue="continuing-test",
        )
        state = await env.client.get_workflow_handle(session_id).query("get_state", result_type=RequirementsState)
        assert state.repository == repository_selection
    messages = [
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": "Topic 1"}, {"role": "assistant", "content": "Question 1?"},
        {"role": "user", "content": "Topic 3"}, {"role": "assistant", "content": "Question 2?"},
    ]
    for index, expected in [(0, []), (2, ["Topic 1"]), (3, ["Topic 1"]),
                            (4, ["Topic 1", "Topic 3"])]:
        requirements = await sessions.state_at_message(
            session_id, messages, index, input_type=RequirementsState,
            default_input=None, activity="requirements_turn", key="requirements",
        )
        assert [item["title"] for item in requirements] == expected
