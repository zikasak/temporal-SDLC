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
    CODEGEN_TASK_QUEUE, DIAGNOSIS_TASK_QUEUE, IMPLEMENTATION_TASK_QUEUE,
    TEST_GENERATION_TASK_QUEUE, VERIFICATION_TASK_QUEUE,
)
from app.delivery import service as delivery_service
from app.delivery.models import DeliveryProgress, delivery_id
from app.delivery.workflows import DeliveryWorkflow
from app.diagnosis.workflows import DiagnosisWorkflow
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
        # Outcomes of successive verifications; the last one repeats.
        self.verification_results: list[str] = []
        self.verification_images = None
        self.verified_images: list[tuple[str, str]] = []
        self.verified_languages: list[str] = []
        # The side each successive diagnosis blames; the last one repeats.
        self.diagnoses = ['implementation']
        self.diagnosed: list[dict[str, Any]] = []
        self.fail_diagnosis = False
        self.repairs: list[tuple[str, dict[str, Any]]] = []
        # What had already run when each repository was checked out.
        self.checkouts: list[tuple[str, bool]] = []
        self.turns = 0
        self.fail_checkout = False
        self.published: list[tuple[str, str]] = []
        self.fail_publication = False
        # The language the model records on its next turn, when any.
        self.language: str | None = None

    @activity.defn(name="checkout_repository")
    async def checkout_repository(self, session_id: str, repository: dict[str, Any]) -> dict[str, Any]:
        if self.fail_checkout:
            raise ApplicationError("clone rejected", non_retryable=True)
        self.checkouts.append((repository["full_name"], self.turns > 0))
        return {"commit": "1" * 40}

    @activity.defn(name="publish_repository")
    async def publish_repository(self, session_id: str, repository: dict[str, Any],
                                 language: str) -> dict[str, Any]:
        if self.fail_publication:
            raise ApplicationError("remote: Permission denied", non_retryable=True)
        self.published.append((session_id, repository["full_name"]))
        return {"branch": "generated/abc", "commit": "2" * 40,
                "url": repository["html_url"] + "/tree/generated/abc"}

    @activity.defn(name="api_design_turn")
    async def api_design_turn(self, state: ApiDesignState) -> dict[str, Any]:
        await self.release_turn.wait()
        self.turns += 1
        api = dict(state.api)
        if not state.messages:
            if self.fail_draft:
                raise ApplicationError("no model", non_retryable=True)
            api["endpoints"] = self.draft_endpoints
            return {"reply": "Draft ready. Who calls the API?", "api": api,
                    "language": state.language}
        # Each message becomes an endpoint path, e.g. "/rooms".
        if self.fail_turn:
            raise ApplicationError("model unavailable", non_retryable=True)
        api["endpoints"] = [*api["endpoints"], endpoint(state.messages[-1]["content"])]
        return {"reply": "Recorded.", "api": api, "language": self.language or state.language}

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
        if 'repair' in context:
            self.repairs.append(('implementation', context['repair']))
            return {"image_id": "sha256:" + "c" * 64}
        return {"image_id": "sha256:" + "a" * 64, "context": context}

    @activity.defn(name="generate_service_tests")
    async def generate_service_tests(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        self.tests_started.set()
        await self.release_tests.wait()
        if self.fail_tests:
            raise ApplicationError("test compilation failed", non_retryable=True)
        if 'repair' in context:
            self.repairs.append(('tests', context['repair']))
            return {"image_id": "sha256:" + "d" * 64}
        return {"image_id": "sha256:" + "b" * 64, "context": context}

    @activity.defn(name='diagnose_failure')
    async def diagnose_failure(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        if self.fail_diagnosis:
            raise ApplicationError('no verdict', non_retryable=True)
        target = self.diagnoses[min(len(self.diagnosed), len(self.diagnoses) - 1)]
        self.diagnosed.append(context)
        return {'session_id': session_id, 'target': target, 'summary': f'The {target} is wrong.',
                'instructions': f'Correct the {target}.', 'path': '/diagnosis'}

    @activity.defn(name='verify_service')
    async def verify_service(self, session_id: str, service_image: str, test_image: str,
                             language: str) -> dict[str, Any]:
        self.verified_languages.append(language)
        self.verification_images = (service_image, test_image)
        self.verified_images.append((service_image, test_image))
        self.verification_started.set()
        await self.release_verification.wait()
        outcome = self.verification_result
        if self.verification_results:
            outcome = self.verification_results[
                min(len(self.verified_images), len(self.verification_results)) - 1]
        if outcome == 'child_error':
            raise ApplicationError('verification worker failed', non_retryable=True)
        failed = outcome == 'failed'
        return {'status': outcome, 'session_id': session_id,
                'tests': 1, 'passed': int(outcome == 'passed'),
                'failures': int(failed), 'errors': 0,
                'skipped': 0, 'duration_seconds': 1.0, 'exit_code': 0,
                'test_failures': [{'class_name': 'RoomsTest', 'name': 'lists', 'kind': 'failure',
                                   'message': 'Expected 200, got 500'}] if failed else [],
                'details_truncated': False, 'service_log': 'NullPointerException' if failed else '',
                'runner_log': '', 'error': None, 'path': '/verification'}


@workflow.defn(name="CodegenWorkflow")
class FakeCodegenWorkflow:
    fail = False
    languages: list[str] = []

    @workflow.run
    async def run(self, session_id: str, document_id: str, language: str
                  ) -> dict[str, Any]:
        FakeCodegenWorkflow.languages.append(language)
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
        env.client, task_queue=TASK_QUEUE, workflows=[ApiDesignWorkflow, DeliveryWorkflow],
        activities=[activities.api_design_turn, activities.write_openapi_document,
                    activities.checkout_repository, activities.publish_repository],
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
    ), Worker(
        env.client, task_queue=DIAGNOSIS_TASK_QUEUE,
        workflows=[DiagnosisWorkflow], activities=[activities.diagnose_failure],
    ):
        FakeCodegenWorkflow.fail = False
        FakeCodegenWorkflow.languages = []
        yield activities


async def start(env, repository=None, language: str | None = "java"):
    state = ApiDesignState(source_session_id="requirements-session-" + "0" * 32,
                           requirements_document="# Requirements", api=empty_api(),
                           language=language, repository=repository)
    return await env.client.start_workflow(
        ApiDesignWorkflow.run, state,
        id=f"api-design-session-{uuid.uuid4().hex}", task_queue=TASK_QUEUE,
    )


@pytest.mark.parametrize("linked", [False, True])
async def test_new_api_design_inherits_the_requirements_repository(env, worker, monkeypatch, repository_selection, linked):
    from app.requirements.models import RequirementsState

    selected = repository_selection if linked else None
    source = RequirementsState(status="completed", document={"document_id": "0" * 32}, repository=selected)

    async def requirements_state(session_id):
        return source

    async def document(*args):
        return {"parsed_text": "# Requirements"}

    async def connect():
        return env.client

    monkeypatch.setattr(service.requirements_service, "get_state", requirements_state)
    monkeypatch.setattr(service, "get_generated_document", document)
    monkeypatch.setattr(service, "connect_temporal", connect)
    monkeypatch.setattr(service, "TASK_QUEUE", TASK_QUEUE)
    if not linked:
        # Only chat may run without a repository.
        with pytest.raises(sessions.SessionBusy, match="no repository"):
            await service.start_session("requirements-session-" + "0" * 32)
        return
    handle = env.client.get_workflow_handle(await service.start_session("requirements-session-" + "0" * 32))
    try:
        state = await wait_for_status(handle, "interviewing")
        assert state.repository == selected
        assert (await service.get_session(handle.id))["repository"] == selected
    finally:
        await handle.terminate()


async def test_repository_is_checked_out_first_and_pushed_once_verified(env, worker, connected, repository_selection):
    worker.release_turn.clear()
    handle = await start(env, repository_selection)
    # Nothing reaches the model, and no message is accepted, before the checkout.
    assert (await handle.query(ApiDesignWorkflow.get_state)).status in {"preparing", "turn_running"}
    worker.release_turn.set()
    await wait_for_status(handle, "interviewing")
    assert worker.checkouts == [(repository_selection["full_name"], False)]
    await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
    worker.fail_publication = True
    await handle.execute_update(ApiDesignWorkflow.finalize)
    state = await delivered(env, handle)
    # Both authors are told to work in the repository the session checked out.
    assert state.implementation["context"]["repository"] == repository_selection
    assert state.tests["context"]["repository"] == repository_selection
    assert state.verification["status"] == "passed"
    assert state.publication is None and state.publication_status == "error"
    assert "Permission denied" in state.publication_error

    # A rejected push is repeated on its own: nothing is generated or verified again.
    worker.fail_publication = False
    await service.retry_generation(handle.id, "repository")
    state = await delivered(env, handle)
    assert state.publication["branch"] == "generated/abc" and state.publication_error is None
    assert state.publication_status == "completed" and state.verification["status"] == "passed"
    assert worker.published == [(handle.id, repository_selection["full_name"])]
    assert len(worker.verified_images) == 1 and len(worker.checkouts) == 1
    with pytest.raises(sessions.SessionBusy):
        await service.retry_generation(handle.id, "repository")


async def test_unverified_sources_are_not_pushed(env, worker, repository_selection):
    worker.verification_result = "error"
    handle = await start(env, repository_selection)
    await wait_for_status(handle, "interviewing")
    await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
    await handle.execute_update(ApiDesignWorkflow.finalize)
    state = await delivered(env, handle)
    assert state.publication_status == "skipped" and not worker.published


async def test_pre_checkout_history_replays_without_checkout_or_push(env, worker, monkeypatch, repository_selection):
    """A session that selected a repository before checkouts existed keeps its history."""
    patched = workflow.patched
    with monkeypatch.context() as legacy:
        legacy.setattr(workflow, 'patched', lambda name: False if name == 'repository-checkout' else patched(name))
        handle = await start(env, repository_selection)
        await wait_for_status(handle, "interviewing")
        await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
        await handle.execute_update(ApiDesignWorkflow.finalize)
        state = await delivered(env, handle)
        assert state.verification['status'] == 'passed' and state.publication_status == 'not_applicable'
        assert not worker.checkouts and not worker.published
        assert 'repository' not in state.implementation['context']
        history = await handle.fetch_history()
    await Replayer(workflows=[ApiDesignWorkflow]).replay_workflow(history)


async def test_session_fails_without_a_model_turn_when_checkout_fails(env, worker, repository_selection):
    worker.fail_checkout = True
    handle = await start(env, repository_selection)
    assert await handle.result() is None
    state = await handle.query(ApiDesignWorkflow.get_state)
    assert state.status == "failed" and "could not be checked out" in state.error
    assert worker.turns == 0 and state.messages == []


def delivery(env, handle) -> Any:
    """The session's delivery; without a run it follows the latest manual retry."""
    return env.client.get_workflow_handle(delivery_id(handle.id), result_type=DeliveryProgress)


async def delivered(env, handle) -> DeliveryProgress:
    assert await handle.result() == DOCUMENT
    return await delivery(env, handle).result()


async def progress(env, handle) -> DeliveryProgress:
    return (await delivery(env, handle).query(DeliveryWorkflow.get_state)).progress


@pytest.fixture
def connected(env, monkeypatch):
    """Point the session services at the test server."""
    async def connect():
        return env.client

    for module in (service, delivery_service, sessions):
        monkeypatch.setattr(module, "connect_temporal", connect)
    monkeypatch.setattr(service, "TASK_QUEUE", TASK_QUEUE)


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


@pytest.mark.parametrize('verification', ['passed', 'error', 'child_error'])
async def test_finalize_writes_document_and_delivers_it(env, worker, verification, connected):
    worker.verification_result = verification
    handle = await start(env)
    await wait_for_status(handle, "interviewing")
    await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
    await handle.execute_update(ApiDesignWorkflow.finalize)
    state = await delivered(env, handle)
    assert worker.documents == [handle.id]
    design = await handle.query(ApiDesignWorkflow.get_state)
    assert design.status == "completed" and design.document == DOCUMENT
    assert state.code == CODE and state.code_error is None
    assert state.implementation["image_id"] == "sha256:" + "a" * 64
    assert state.implementation["context"]["requirements_document"] == "# Requirements"
    assert state.implementation["context"]["api_design"]["endpoints"][0]["path"] == "/rooms"
    assert state.tests["image_id"] == "sha256:" + "b" * 64
    assert state.tests_error is None
    assert state.tests["context"] == {
        "requirements": [], "requirements_document": "# Requirements", "api_design": design.api,
        "language": "java",
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

    # The session API reports the design and its delivery as one session.
    response = await service.get_session(handle.id)
    assert response['status'] == 'completed' and response['code'] == CODE
    assert response['verification'] == state.verification
    assert response['verification_status'] == state.verification_status
    assert response['verification_error'] == state.verification_error


async def finalized(env) -> DeliveryProgress:
    """The outcome of delivering a one-endpoint design."""
    handle = await start(env)
    await wait_for_status(handle, 'interviewing')
    await handle.execute_update(ApiDesignWorkflow.send_message, '/rooms')
    await handle.execute_update(ApiDesignWorkflow.finalize)
    return await delivered(env, handle)


async def test_failed_tests_send_the_diagnosed_side_back_until_verification_passes(env, worker):
    worker.verification_results = ['failed', 'failed', 'passed']
    worker.diagnoses = ['tests', 'implementation']
    state = await finalized(env)
    assert state.verification['status'] == 'passed'
    assert state.verification_status == 'completed' and state.verification_error is None
    assert [(item['attempt'], item['target']) for item in state.repairs] == [
        (1, 'tests'), (2, 'implementation')]
    assert state.repairs[0]['summary'] == 'The tests is wrong.'
    # Every diagnosis sees the contracts and the verification that just failed.
    assert [item['verification']['status'] for item in worker.diagnosed] == ['failed', 'failed']
    assert worker.diagnosed[0]['api_design']['endpoints'][0]['path'] == '/rooms'
    assert worker.diagnosed[0]['requirements_document'] == '# Requirements'
    # Only the blamed side is rewritten each round, with the verdict and its failing tests.
    assert [stage for stage, _ in worker.repairs] == ['tests', 'implementation']
    tests_repair, service_repair = (repair for _, repair in worker.repairs)
    assert tests_repair['instructions'] == 'Correct the tests.'
    assert tests_repair['test_failures'][0]['name'] == 'lists'
    assert 'service_log' not in tests_repair
    assert service_repair['service_log'] == 'NullPointerException'
    # Each verification runs the images as they stand after the preceding repair.
    original, repaired = ('sha256:' + 'a' * 64, 'sha256:' + 'b' * 64), ('sha256:' + 'c' * 64, 'sha256:' + 'd' * 64)
    assert worker.verified_images == [original, (original[0], repaired[1]), repaired]
    assert (state.implementation['image_id'], state.tests['image_id']) == repaired


async def test_repair_stops_after_the_round_limit_with_the_last_failure(env, worker):
    worker.verification_result = 'failed'
    state = await finalized(env)
    assert len(state.repairs) == len(worker.repairs) == 5
    assert len(worker.verified_images) == 6
    assert state.verification['status'] == 'failed' and state.verification_status == 'completed'
    assert state.verification_error == 'Tests still fail after 5 automatic repair rounds.'


@pytest.mark.parametrize('broken', ['diagnosis', 'implementation'])
async def test_failed_diagnosis_or_repair_keeps_the_published_images(env, worker, broken, connected):
    worker.verification_results = ['failed', 'passed']
    worker.fail_diagnosis = broken == 'diagnosis'
    worker.release_verification.clear()
    handle = await start(env)
    await wait_for_status(handle, 'interviewing')
    await handle.execute_update(ApiDesignWorkflow.send_message, '/rooms')
    await handle.execute_update(ApiDesignWorkflow.finalize)
    await asyncio.wait_for(worker.verification_started.wait(), timeout=10)
    # The first build succeeded; only the repair of it fails.
    worker.fail_implementation = broken == 'implementation'
    worker.release_verification.set()
    state = await delivered(env, handle)
    assert len(worker.verified_images) == 1 and not worker.repairs
    assert state.verification['status'] == 'failed' and state.verification_status == 'completed'
    assert state.implementation['image_id'] == 'sha256:' + 'a' * 64
    if broken == 'diagnosis':
        assert state.verification_error == 'The failed tests could not be diagnosed.'
        assert state.repairs == [] and state.implementation_error is None
        return
    assert state.implementation_error == 'The service could not be implemented or built.'

    # The failed repair is recoverable through the existing manual retry, which
    # rebuilds the service and resumes verification with a clean repair history.
    worker.fail_implementation = False
    await service.retry_generation(handle.id, 'server')
    state = await delivered(env, handle)
    assert state.verification['status'] == 'passed' and state.repairs == []


@pytest.mark.parametrize('failed', [{'implementation'}, {'tests'}, {'implementation', 'tests'}])
async def test_failed_coding_branches_preserve_other_artifacts(env, worker, failed, connected, repository_selection):
    worker.fail_implementation = 'implementation' in failed
    worker.fail_tests = 'tests' in failed
    handle = await start(env, repository_selection)
    await wait_for_status(handle, "interviewing")
    await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
    await handle.execute_update(ApiDesignWorkflow.finalize)
    state = await delivered(env, handle)
    assert state.code == CODE
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

    # A fresh delivery run retries only the requested failed stage, without
    # regenerating successful siblings.
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
        still_failed = await delivered(env, handle)
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
        await asyncio.wait_for(getattr(worker, f'{branch}_started').wait(), timeout=10)
        with pytest.raises(sessions.SessionBusy):
            await service.retry_generation(handle.id, stage)
        pending = await service.get_session(handle.id)
        assert pending[f'{branch}_error'] is None
        assert pending['verification_status'] == 'waiting'
        getattr(worker, f'release_{branch}').set()
        state = await delivered(env, handle)
        assert getattr(state, branch)['image_id']
        assert getattr(state, f'{branch}_error') is None
        assert not getattr(worker, f'{sibling}_started').is_set()
        assert state.code == CODE
        # The retried stage still works in the session's repository.
        assert getattr(state, branch)['context']['repository'] == repository_selection
        assert worker.documents == [handle.id]
    assert state.verification_status == 'completed'
    assert worker.verification_started.is_set()


async def test_generation_retry_http_validation(env, worker, connected):
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
        state = await progress(env, handle)
        assert state.implementation is None and state.tests is None
        assert state.verification_status == 'waiting'
        assert not worker.verification_started.is_set()
        # Either result is observable while the other branch remains running;
        # verification must not start until the second one succeeds.
        getattr(worker, f'release_{first}').set()
        for _ in range(100):
            state = await progress(env, handle)
            if getattr(state, first) is not None:
                break
            await asyncio.sleep(0.05)
        assert getattr(state, first) is not None
        assert state.verification is None and not worker.verification_started.is_set()
        worker.release_implementation.set()
        worker.release_tests.set()
        await asyncio.wait_for(worker.verification_started.wait(), timeout=10)
        assert (await progress(env, handle)).verification_status == 'running'
    finally:
        worker.release_implementation.set()
        worker.release_tests.set()
        worker.release_verification.set()
        await delivered(env, handle)


async def test_failed_code_generation_can_be_retried(env, worker, connected):
    FakeCodegenWorkflow.fail = True
    handle = await start(env)
    await wait_for_status(handle, "interviewing")
    await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
    await handle.execute_update(ApiDesignWorkflow.finalize)
    state = await delivered(env, handle)
    assert state.code is None
    assert state.code_error == "The server code could not be generated."
    assert not worker.implementation_started.is_set() and not worker.tests_started.is_set()
    assert state.verification_status == 'skipped'
    assert not worker.verification_started.is_set()

    with pytest.raises(sessions.SessionBusy):
        await service.retry_generation(handle.id, 'tests')
    FakeCodegenWorkflow.fail = False
    await service.retry_generation(handle.id, 'server')
    state = await delivered(env, handle)
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
    assert (await delivered(env, handle)).code == CODE


async def test_language_must_be_chosen_before_code_is_generated(env, worker):
    handle = await start(env, language=None)
    await wait_for_status(handle, "interviewing")
    await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
    with pytest.raises(WorkflowUpdateFailedError) as refused:
        await handle.execute_update(ApiDesignWorkflow.finalize)
    assert "implementation language is not chosen" in str(refused.value.cause)

    worker.language = "java"
    await handle.execute_update(ApiDesignWorkflow.send_message, "/bookings")
    await handle.execute_update(ApiDesignWorkflow.finalize)
    assert (await delivered(env, handle)).code == CODE
    assert (await handle.query(ApiDesignWorkflow.get_state)).language == "java"
    assert FakeCodegenWorkflow.languages == ["java"]
    assert worker.verified_languages == ["java"]


async def test_idle_session_without_a_language_keeps_the_document_only(env, worker, connected):
    handle = await start(env, language=None)
    await wait_for_status(handle, "interviewing")
    await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
    await env.sleep(timedelta(hours=25))
    state = await delivered(env, handle)
    assert state.code is None and FakeCodegenWorkflow.languages == []
    assert state.code_error == "No implementation language was chosen for this session."
    assert state.verification_status == 'skipped'

    # Retrying cannot choose a language, so it is refused instead of restarting
    # a run that would only fail the same way.
    first_run = (await delivery(env, handle).describe()).run_id
    with pytest.raises(sessions.SessionBusy, match="Fork it"):
        await service.retry_generation(handle.id, 'server')
    assert (await delivery(env, handle).describe()).run_id == first_run


async def test_idle_session_with_problems_fails(env, worker):
    worker.draft_endpoints = [endpoint("/rooms", "Room")]
    handle = await start(env)
    await wait_for_status(handle, "interviewing")
    await env.sleep(timedelta(hours=25))
    assert await handle.result() is None
    state = await handle.query(ApiDesignWorkflow.get_state)
    assert state.status == "failed" and worker.documents == []


async def test_fork_keeps_api_as_it_was_at_the_message(env, worker, connected, repository_selection):
    worker.draft_endpoints = [endpoint("/rooms")]
    source = await start(env, repository_selection)
    await wait_for_status(source, "interviewing")
    await source.execute_update(ApiDesignWorkflow.send_message, "/bookings")
    # draft, /bookings, Recorded.

    fork = env.client.get_workflow_handle(await service.fork_session(source.id, 0))
    # A fork checks out its own copy of the repository first.
    state = await wait_for_status(fork, "interviewing")
    assert [item["path"] for item in state.api["endpoints"]] == ["/rooms"]
    assert state.requirements_document == "# Requirements"
    assert state.forked_from == {"session_id": source.id, "message_index": 0}
    assert (await service.get_session(fork.id))["repository"] == repository_selection

    # Forking after a user message answers it again from the API it had then.
    regenerated = env.client.get_workflow_handle(await service.fork_session(source.id, 1))
    state = await wait_for_status(regenerated, "interviewing")
    assert len(state.messages) == 3
    assert [item["path"] for item in state.api["endpoints"]] == ["/rooms", "/bookings"]
    assert state.repository == repository_selection
    nested = env.client.get_workflow_handle(await service.fork_session(fork.id, 0))
    assert (await wait_for_status(nested, "interviewing")).repository == repository_selection
    await nested.terminate()
    for handle in (source, fork, regenerated):
        await handle.terminate()
