import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from temporalio import activity, workflow
from temporalio.client import WorkflowUpdateFailedError, WorkflowUpdateStage
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from app.api.routes.api_design import router
from app.api_design import service
from app.api_design.models import ApiDesignState, empty_api
from app.api_design.prompt import OPENING_FALLBACK
from app.config import (
    CODEGEN_TASK_QUEUE, IMPLEMENTATION_TASK_QUEUE, TEST_GENERATION_TASK_QUEUE,
    VERIFICATION_TASK_QUEUE,
)
from app.implementation.workflows import ImplementationWorkflow
from app.temporal import sessions
from app.temporal.workflows import ApiDesignWorkflow
from app.test_generation.workflows import TestGenerationWorkflow as ServiceTestWorkflow
from app.verification.workflows import VerificationWorkflow

TASK_QUEUE = "api-design-test"
DOCUMENT = {"document_id": "0" * 32, "filename": "openapi.yaml",
            "content_type": "application/yaml", "size_bytes": 10, "revision": 1}
CODE = {"server": {"path": "/generated/server", "files": 8}}


def endpoint(path: str, response_schema: str | None = None) -> dict[str, Any]:
    return {
        "method": "GET", "path": path, "summary": "S", "description": "", "tag": "",
        "parameters": [], "request_schema": None, "response_status": 200,
        "response_schema": response_schema, "response_array": False,
        "error_statuses": [], "requirement_ids": [],
    }


class FakeActivities:
    def __init__(self) -> None:
        self.release_turn = asyncio.Event()
        self.release_turn.set()
        self.fail_draft = False
        self.fail_turn = False
        self.draft_endpoints: list[dict[str, Any]] = []
        self.documents: list[str] = []
        self.fail_implementation = False
        self.fail_tests = False
        self.implementation_started = asyncio.Event()
        self.tests_started = asyncio.Event()
        self.release_implementation = asyncio.Event()
        self.release_implementation.set()
        self.release_tests = asyncio.Event()
        self.release_tests.set()
        self.verification_started = asyncio.Event()
        self.release_verification = asyncio.Event()
        self.release_verification.set()
        self.verification_result = 'passed'
        self.verification_images = None

    @activity.defn(name="api_design_turn")
    async def api_design_turn(self, state: ApiDesignState) -> dict[str, Any]:
        await self.release_turn.wait()
        api = dict(state.api)
        if not state.messages:
            if self.fail_draft:
                raise ApplicationError("no model", non_retryable=True)
            api["endpoints"] = self.draft_endpoints
            return {"reply": "Draft ready. Who calls the API?", "api": api}
        # Each message becomes an endpoint path, e.g. "/rooms".
        if self.fail_turn:
            raise ApplicationError("model unavailable", non_retryable=True)
        api["endpoints"] = [*api["endpoints"], endpoint(state.messages[-1]["content"])]
        return {"reply": "Recorded.", "api": api}

    @activity.defn(name="write_openapi_document")
    async def write_openapi_document(self, session_id: str, api: dict[str, Any]
                                     ) -> dict[str, Any]:
        self.documents.append(session_id)
        return DOCUMENT

    @activity.defn(name="implement_service")
    async def implement_service(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        self.implementation_started.set()
        await self.release_implementation.wait()
        if self.fail_implementation:
            raise ApplicationError("build failed", non_retryable=True)
        return {"image_id": "sha256:" + "a" * 64, "context": context}

    @activity.defn(name="generate_service_tests")
    async def generate_service_tests(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        self.tests_started.set()
        await self.release_tests.wait()
        if self.fail_tests:
            raise ApplicationError("test compilation failed", non_retryable=True)
        return {"image_id": "sha256:" + "b" * 64, "context": context}

    @activity.defn(name='verify_service')
    async def verify_service(self, session_id: str, service_image: str,
                             test_image: str) -> dict[str, Any]:
        self.verification_images = (service_image, test_image)
        self.verification_started.set()
        await self.release_verification.wait()
        if self.verification_result == 'child_error':
            raise ApplicationError('verification worker failed', non_retryable=True)
        return {'status': self.verification_result, 'session_id': session_id,
                'tests': 1, 'passed': int(self.verification_result == 'passed'),
                'failures': int(self.verification_result == 'failed'), 'errors': 0,
                'skipped': 0, 'duration_seconds': 1.0, 'exit_code': 0,
                'test_failures': [], 'details_truncated': False,
                'service_log': '', 'runner_log': '', 'error': None, 'path': '/verification'}


@workflow.defn(name="CodegenWorkflow")
class FakeCodegenWorkflow:
    fail = False

    @workflow.run
    async def run(self, session_id: str, document_id: str) -> dict[str, Any]:
        if FakeCodegenWorkflow.fail:
            raise ApplicationError("generator failed", non_retryable=True)
        return CODE


@pytest.fixture(scope="session")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@pytest.fixture
async def worker(env):
    activities = FakeActivities()
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[ApiDesignWorkflow],
        activities=[activities.api_design_turn, activities.write_openapi_document],
    ), Worker(
        # The fake lives in this module, whose imports the sandbox would reject.
        env.client, task_queue=CODEGEN_TASK_QUEUE, workflows=[FakeCodegenWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ), Worker(
        env.client, task_queue=IMPLEMENTATION_TASK_QUEUE,
        workflows=[ImplementationWorkflow], activities=[activities.implement_service],
    ), Worker(
        env.client, task_queue=TEST_GENERATION_TASK_QUEUE,
        workflows=[ServiceTestWorkflow], activities=[activities.generate_service_tests],
    ), Worker(
        env.client, task_queue=VERIFICATION_TASK_QUEUE,
        workflows=[VerificationWorkflow], activities=[activities.verify_service],
    ):
        FakeCodegenWorkflow.fail = False
        yield activities


async def start(env):
    state = ApiDesignState(source_session_id="requirements-session-" + "0" * 32,
                           requirements_document="# Requirements", api=empty_api())
    return await env.client.start_workflow(
        ApiDesignWorkflow.run, state,
        id=f"api-design-session-{uuid.uuid4().hex}", task_queue=TASK_QUEUE,
    )


async def wait_for_status(handle, status: str) -> ApiDesignState:
    for _ in range(100):
        state = await handle.query(ApiDesignWorkflow.get_state)
        if state.status == status:
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(f"status never became {status}")


async def test_draft_discuss_and_finalize(env, worker):
    worker.draft_endpoints = [endpoint("/rooms", "Room")]
    handle = await start(env)
    state = await wait_for_status(handle, "interviewing")
    assert state.messages == [{"role": "assistant", "content": "Draft ready. Who calls the API?"}]
    assert state.api["endpoints"][0]["path"] == "/rooms"

    # The draft refers to a schema nobody recorded, so finalizing is refused.
    with pytest.raises(WorkflowUpdateFailedError) as refused:
        await handle.execute_update(ApiDesignWorkflow.finalize)
    assert "unknown schema Room" in str(refused.value.cause)

    await handle.execute_update(ApiDesignWorkflow.send_message, "  /bookings  ")
    state = await handle.query(ApiDesignWorkflow.get_state)
    assert [item["role"] for item in state.messages] == ["assistant", "user", "assistant"]
    assert state.messages[1]["content"] == "/bookings"
    assert len(state.api["endpoints"]) == 2
    await handle.terminate()


@pytest.mark.parametrize('verification', ['passed', 'failed', 'error', 'child_error'])
async def test_finalize_writes_document(env, worker, verification, monkeypatch):
    worker.verification_result = verification
    handle = await start(env)
    await wait_for_status(handle, "interviewing")
    await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
    await handle.execute_update(ApiDesignWorkflow.finalize)
    assert await handle.result() == DOCUMENT
    assert worker.documents == [handle.id]
    state = await handle.query(ApiDesignWorkflow.get_state)
    assert state.status == "completed" and state.document == DOCUMENT
    assert state.code == CODE and state.code_error is None
    assert state.implementation["image_id"] == "sha256:" + "a" * 64
    assert state.implementation["context"]["requirements_document"] == "# Requirements"
    assert state.implementation["context"]["api_design"]["endpoints"][0]["path"] == "/rooms"
    assert state.tests["image_id"] == "sha256:" + "b" * 64
    assert state.tests_error is None
    assert state.tests["context"] == {
        "requirements": [], "requirements_document": "# Requirements", "api_design": state.api,
    }
    assert worker.verification_images == ('sha256:' + 'a' * 64, 'sha256:' + 'b' * 64)
    if verification == 'child_error':
        assert state.verification is None
        assert state.verification_status == 'error'
        assert state.verification_error == 'Service verification could not complete.'
    else:
        assert state.verification['status'] == verification
        assert state.verification_status == 'completed'
        assert state.verification_error is None

    async def connect():
        return env.client

    monkeypatch.setattr(service, 'connect_temporal', connect)
    response = await service.get_session(handle.id)
    assert response['verification'] == state.verification
    assert response['verification_status'] == state.verification_status
    assert response['verification_error'] == state.verification_error


@pytest.mark.parametrize('failed', [{'implementation'}, {'tests'}, {'implementation', 'tests'}])
async def test_failed_coding_branches_preserve_other_artifacts(env, worker, failed, monkeypatch):
    worker.fail_implementation = 'implementation' in failed
    worker.fail_tests = 'tests' in failed
    handle = await start(env)
    await wait_for_status(handle, "interviewing")
    await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
    await handle.execute_update(ApiDesignWorkflow.finalize)
    assert await handle.result() == DOCUMENT
    state = await handle.query(ApiDesignWorkflow.get_state)
    assert state.code == CODE and state.document == DOCUMENT
    assert state.verification_status == 'skipped' and state.verification is None
    assert not worker.verification_started.is_set()
    if 'implementation' in failed:
        assert state.implementation is None
        assert state.implementation_error == "The service could not be implemented or built."
    else:
        assert state.implementation["image_id"] == "sha256:" + "a" * 64
        assert state.implementation_error is None
    if 'tests' in failed:
        assert state.tests is None
        assert state.tests_error == "The service tests could not be generated or built."
    else:
        assert state.tests["image_id"] == "sha256:" + "b" * 64
        assert state.tests_error is None

    async def connect():
        return env.client

    monkeypatch.setattr(service, 'connect_temporal', connect)
    # A fresh run retries only the requested failed stage, without redrafting
    # the API or regenerating successful siblings.
    for branch in sorted(failed):
        getattr(worker, f'{branch}_started').clear()
        sibling = 'tests' if branch == 'implementation' else 'implementation'
        getattr(worker, f'{sibling}_started').clear()
        stage = 'server' if branch == 'implementation' else 'tests'
        # Refuse a successful stage and a second retry while this one runs.
        if sibling not in failed:
            with pytest.raises(sessions.SessionBusy):
                await service.retry_generation(handle.id, 'tests' if sibling == 'tests' else 'server')
        # Another failure must remain retryable, including across closed runs.
        await service.retry_generation(handle.id, stage)
        unsuccessful = env.client.get_workflow_handle(handle.id)
        assert await unsuccessful.result() == DOCUMENT
        still_failed = await unsuccessful.query(ApiDesignWorkflow.get_state)
        assert getattr(still_failed, f'{branch}_error')
        assert still_failed.verification_status == 'skipped'
        assert not getattr(worker, f'{sibling}_started').is_set()
        getattr(worker, f'{branch}_started').clear()
        getattr(worker, f'release_{branch}').clear()
        setattr(worker, f'fail_{branch}', False)
        attempts = await asyncio.gather(
            service.retry_generation(handle.id, stage),
            service.retry_generation(handle.id, stage),
            return_exceptions=True,
        )
        assert sum(result is None for result in attempts) == 1
        assert sum(isinstance(result, sessions.SessionBusy) for result in attempts) == 1
        retry = env.client.get_workflow_handle(handle.id)
        await asyncio.wait_for(getattr(worker, f'{branch}_started').wait(), timeout=10)
        with pytest.raises(sessions.SessionBusy):
            await service.retry_generation(handle.id, stage)
        pending = await service.get_session(handle.id)
        assert pending[f'{branch}_error'] is None
        assert pending['verification_status'] == 'waiting'
        getattr(worker, f'release_{branch}').set()
        assert await retry.result() == DOCUMENT
        state = await retry.query(ApiDesignWorkflow.get_state)
        assert getattr(state, branch)['image_id']
        assert getattr(state, f'{branch}_error') is None
        assert not getattr(worker, f'{sibling}_started').is_set()
        assert state.document == DOCUMENT and state.code == CODE
        assert worker.documents == [handle.id]
    assert state.verification_status == 'completed'
    assert worker.verification_started.is_set()
    # Forking an earlier answer must still use the pre-retry design history.
    monkeypatch.setattr(sessions, 'connect_temporal', connect)
    monkeypatch.setattr(service, 'TASK_QUEUE', TASK_QUEUE)
    fork = env.client.get_workflow_handle(await service.fork_session(handle.id, 0))
    try:
        fork_state = await fork.query(ApiDesignWorkflow.get_state)
        assert fork_state.api['endpoints'] == []
    finally:
        await fork.terminate()


async def test_generation_retry_http_validation(env, worker, monkeypatch):
    async def connect():
        return env.client

    monkeypatch.setattr(service, 'connect_temporal', connect)
    app = FastAPI()
    app.include_router(router, prefix='/api')
    handle = await start(env)
    await wait_for_status(handle, 'interviewing')
    path = f'/api/api-design/sessions/{handle.id}/generation/retry'
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
            for body in ({}, {'stage': 'verification'}):
                response = await http.post(path, json=body)
                assert response.status_code == 422
            response = await http.post(path, json={'stage': 'server'})
            assert response.status_code == 409
            missing = f'/api/api-design/sessions/api-design-session-{uuid.uuid4().hex}/generation/retry'
            response = await http.post(missing, json={'stage': 'tests'})
            assert response.status_code == 404
    finally:
        await handle.terminate()


@pytest.mark.parametrize('first', ['tests', 'implementation'])
async def test_implementation_and_tests_start_before_either_finishes(env, worker, first):
    """Sequential children would leave one activity waiting forever at this barrier."""
    worker.release_implementation.clear()
    worker.release_tests.clear()
    worker.release_verification.clear()
    handle = await start(env)
    try:
        await wait_for_status(handle, "interviewing")
        await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
        await handle.execute_update(ApiDesignWorkflow.finalize)
        await asyncio.wait_for(asyncio.gather(
            worker.implementation_started.wait(), worker.tests_started.wait(),
        ), timeout=10)
        state = await handle.query(ApiDesignWorkflow.get_state)
        assert state.implementation is None and state.tests is None
        assert state.verification_status == 'waiting'
        assert not worker.verification_started.is_set()
        # Either result is observable while the other branch remains running;
        # verification must not start until the second one succeeds.
        getattr(worker, f'release_{first}').set()
        for _ in range(100):
            state = await handle.query(ApiDesignWorkflow.get_state)
            if getattr(state, first) is not None:
                break
            await asyncio.sleep(0.05)
        assert getattr(state, first) is not None
        assert state.verification is None and not worker.verification_started.is_set()
        worker.release_implementation.set()
        worker.release_tests.set()
        await asyncio.wait_for(worker.verification_started.wait(), timeout=10)
        state = await handle.query(ApiDesignWorkflow.get_state)
        assert state.verification_status == 'running'
    finally:
        worker.release_implementation.set()
        worker.release_tests.set()
        worker.release_verification.set()
        await handle.result()


async def test_pre_verification_history_replays_without_new_child(env, worker, monkeypatch):
    """A history with the old command sequence must not schedule verification on replay."""
    patched = workflow.patched
    with monkeypatch.context() as legacy:
        legacy.setattr(workflow, 'patched', lambda name: False if name == 'verification-child-workflow' else patched(name))
        handle = await start(env)
        await wait_for_status(handle, 'interviewing')
        await handle.execute_update(ApiDesignWorkflow.send_message, '/rooms')
        await handle.execute_update(ApiDesignWorkflow.finalize)
        assert await handle.result() == DOCUMENT
        state = await handle.query(ApiDesignWorkflow.get_state)
        assert state.verification_status == 'not_applicable'
        assert not worker.verification_started.is_set()
        history = await handle.fetch_history()
    await Replayer(workflows=[ApiDesignWorkflow]).replay_workflow(history)


async def test_failed_code_generation_keeps_session_completed(env, worker, monkeypatch):
    FakeCodegenWorkflow.fail = True
    handle = await start(env)
    await wait_for_status(handle, "interviewing")
    await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
    await handle.execute_update(ApiDesignWorkflow.finalize)
    assert await handle.result() == DOCUMENT
    state = await handle.query(ApiDesignWorkflow.get_state)
    assert state.status == "completed" and state.code is None
    assert state.code_error == "The server code could not be generated."
    assert not worker.implementation_started.is_set() and not worker.tests_started.is_set()
    assert state.verification_status == 'skipped'
    assert not worker.verification_started.is_set()

    async def connect():
        return env.client

    monkeypatch.setattr(service, 'connect_temporal', connect)
    with pytest.raises(sessions.SessionBusy):
        await service.retry_generation(handle.id, 'tests')
    FakeCodegenWorkflow.fail = False
    await service.retry_generation(handle.id, 'server')
    retry = env.client.get_workflow_handle(handle.id)
    assert await retry.result() == DOCUMENT
    state = await retry.query(ApiDesignWorkflow.get_state)
    assert state.code == CODE and state.code_error is None
    assert state.implementation and state.tests
    assert state.verification_status == 'completed'
    assert worker.documents == [handle.id]


async def test_failed_draft_falls_back_to_opening_question(env, worker):
    worker.fail_draft = True
    handle = await start(env)
    state = await wait_for_status(handle, "interviewing")
    assert state.messages == [{"role": "assistant", "content": OPENING_FALLBACK}]
    assert state.error is None
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(ApiDesignWorkflow.finalize)
    await handle.terminate()


async def test_rejects_message_while_turn_running(env, worker):
    handle = await start(env)
    await wait_for_status(handle, "interviewing")
    worker.release_turn.clear()
    first = await handle.start_update(
        ApiDesignWorkflow.send_message, "/first",
        wait_for_stage=WorkflowUpdateStage.ACCEPTED,
    )
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(ApiDesignWorkflow.send_message, "/second")
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(ApiDesignWorkflow.finalize)
    worker.release_turn.set()
    await first.result()
    state = await handle.query(ApiDesignWorkflow.get_state)
    assert [item["content"] for item in state.messages if item["role"] == "user"] == ["/first"]
    await handle.terminate()


async def test_failed_turn_keeps_message_and_can_retry(env, worker):
    handle = await start(env)
    await wait_for_status(handle, "interviewing")
    worker.fail_turn = True
    await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
    state = await handle.query(ApiDesignWorkflow.get_state)
    assert state.messages[-1] == {"role": "user", "content": "/rooms"}
    assert state.error and state.status == "interviewing"
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(ApiDesignWorkflow.send_message, "/other")
    worker.fail_turn = False
    await handle.execute_update(ApiDesignWorkflow.retry_message)
    state = await handle.query(ApiDesignWorkflow.get_state)
    assert [item["role"] for item in state.messages] == ["assistant", "user", "assistant"]
    assert state.api["endpoints"][0]["path"] == "/rooms" and state.error is None
    await handle.terminate()


async def test_idle_session_with_complete_api_finalizes(env, worker):
    handle = await start(env)
    await wait_for_status(handle, "interviewing")
    await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
    await env.sleep(timedelta(hours=25))
    assert await handle.result() == DOCUMENT


async def test_idle_session_with_problems_fails(env, worker):
    worker.draft_endpoints = [endpoint("/rooms", "Room")]
    handle = await start(env)
    await wait_for_status(handle, "interviewing")
    await env.sleep(timedelta(hours=25))
    assert await handle.result() is None
    state = await handle.query(ApiDesignWorkflow.get_state)
    assert state.status == "failed" and worker.documents == []


async def test_fork_keeps_api_as_it_was_at_the_message(env, worker, monkeypatch):
    async def connect():
        return env.client

    monkeypatch.setattr(service, "connect_temporal", connect)
    monkeypatch.setattr(sessions, "connect_temporal", connect)
    monkeypatch.setattr(service, "TASK_QUEUE", TASK_QUEUE)
    worker.draft_endpoints = [endpoint("/rooms")]
    source = await start(env)
    await wait_for_status(source, "interviewing")
    await source.execute_update(ApiDesignWorkflow.send_message, "/bookings")
    # draft, /bookings, Recorded.

    fork = env.client.get_workflow_handle(await service.fork_session(source.id, 0))
    state = await fork.query(ApiDesignWorkflow.get_state)
    assert [item["path"] for item in state.api["endpoints"]] == ["/rooms"]
    assert state.requirements_document == "# Requirements"
    assert state.forked_from == {"session_id": source.id, "message_index": 0}

    # Forking after a user message answers it again from the API it had then.
    regenerated = env.client.get_workflow_handle(await service.fork_session(source.id, 1))
    state = await wait_for_status(regenerated, "interviewing")
    assert len(state.messages) == 3
    assert [item["path"] for item in state.api["endpoints"]] == ["/rooms", "/bookings"]
    for handle in (source, fork, regenerated):
        await handle.terminate()
