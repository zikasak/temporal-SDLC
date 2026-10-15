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
from temporalio.worker import Replayer, Worker

from app.api.routes.api_design import router
from app.api_design import service
from app.api_design.models import ApiDesignState, empty_api
from app.api_design.prompt import OPENING_FALLBACK, OPENING_QUESTIONS
from app.delivery import service as delivery_service
from app.delivery.models import DeliveryProgress, delivery_id
from app.delivery.workflows import DeliveryWorkflow
from app.requirements.document import render_requirements_markdown
from app.temporal import sessions
from app.temporal.workflows import ApiDesignWorkflow
from tests.pipeline import CODE, FakeCodegenWorkflow, FakeStages, stage_workers

TASK_QUEUE = "api-design-test"
DOCUMENT = {"document_id": "0" * 32, "filename": "openapi.yaml",
            "content_type": "application/yaml", "size_bytes": 10, "revision": 1}


def endpoint(path: str, response_schema: str | None = None) -> dict[str, Any]:
    return {
        "method": "GET", "path": path, "summary": "S", "description": "", "tag": "",
        "parameters": [], "request_schema": None, "response_status": 200,
        "response_schema": response_schema, "response_array": False,
        "error_statuses": [], "requirement_ids": [],
    }


class FakeActivities(FakeStages):
    def __init__(self) -> None:
        super().__init__()
        self.release_turn = asyncio.Event()
        self.release_turn.set()
        self.fail_draft = False
        self.fail_turn = False
        self.draft_endpoints: list[dict[str, Any]] = []
        self.documents: list[str] = []
        # The language the model records on its next turn, when any.
        self.language: str | None = None
        # What the contract files of the session's own checkout hold.
        self.repository_contract: dict[str, Any] | None = None

    @activity.defn(name="read_repository_contract")
    async def read_repository_contract(self, session_id: str) -> dict[str, Any]:
        if self.repository_contract is None:
            raise ApplicationError("no contract", type="ContractError", non_retryable=True)
        return self.repository_contract

    @activity.defn(name="api_design_turn")
    async def api_design_turn(self, state: ApiDesignState) -> dict[str, Any]:
        await self.release_turn.wait()
        self.turns += 1
        api = dict(state.api)
        if not state.messages:
            if self.fail_draft:
                raise ApplicationError("no model", non_retryable=True)
            api["endpoints"] = [*api["endpoints"], *self.draft_endpoints]
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
                    activities.checkout_repository, activities.publish_repository,
                    activities.read_repository_contract],
    ), stage_workers(env, activities):
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
    source = RequirementsState(status="completed", document={"document_id": "0" * 32}, repository=selected,
                               kind="new_service")

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
        assert state.repository == selected and state.kind == "new_service"
        assert (await service.get_session(handle.id))["repository"] == selected
    finally:
        await handle.terminate()


def requirement(number: int, title: str) -> dict[str, Any]:
    return {"id": f"FR-{number}", "category": "functional", "title": title,
            "description": "D", "priority": "must"}


@pytest.fixture
def change(monkeypatch, connected, repository_selection):
    """A finished requirements session that changes the API of a published service: it
    adds FR-3 and ends FR-2, which one of the service's endpoints is linked to."""
    from app.requirements.models import RequirementsState

    published = [requirement(1, "Book rooms"), requirement(2, "List rooms by floor")]
    contract = {"language": "java", "requirements": published,
                "requirements_document": render_requirements_markdown(published, "A hotel."),
                "api": {**empty_api(), "endpoints": [
                    {**endpoint("/rooms"), "requirement_ids": ["FR-1", "FR-2"]}]}}
    source = RequirementsState(
        status="completed", document={"document_id": "0" * 32}, kind="api_change",
        repository=repository_selection, requirements=[requirement(3, "Cancel bookings")],
        retired=published[1:], contract=contract,
        # The commit the fake checkout of every session reports.
        contract_commit="1" * 40)

    async def requirements_state(session_id):
        return source

    async def document(*args):
        return {"parsed_text": "# Change"}

    monkeypatch.setattr(service.requirements_service, "get_state", requirements_state)
    monkeypatch.setattr(service, "get_generated_document", document)
    return source


async def test_api_change_is_designed_from_the_repository_contract(env, worker, change):
    worker.draft_endpoints = [endpoint("/bookings")]
    handle = env.client.get_workflow_handle(await service.start_session("requirements-session-" + "0" * 32))
    state = await wait_for_status(handle, "interviewing")
    # The designer starts from the API and the language the service has, not from nothing.
    assert [item["path"] for item in state.api["endpoints"]] == ["/rooms", "/bookings"]
    assert state.language == "java" and state.change_document == "# Change"
    # What the change ends is gone from the requirements and from the endpoints' links.
    required = [change.contract["requirements"][0], *change.requirements]
    assert state.requirements == required
    assert state.api["endpoints"][0]["requirement_ids"] == ["FR-1"]

    # A fork changes the same service.
    fork = env.client.get_workflow_handle(await service.fork_session(handle.id, 0))
    forked = await wait_for_status(fork, "interviewing")
    assert forked.change_document == "# Change" and forked.api == state.api
    await fork.terminate()

    await handle.execute_update(ApiDesignWorkflow.finalize)
    progress = await delivered(env, handle)
    # The service is built and published with the contract as the change leaves it.
    merged = render_requirements_markdown(required, "A hotel.")
    assert worker.contract == {"language": "java", "requirements": required,
                               "requirements_document": merged, "api": state.api}
    assert worker.published_kind == "api_change"
    assert progress.implementation["context"]["requirements_document"] == merged


@pytest.mark.parametrize("same_contract", [True, False])
async def test_api_change_goes_on_from_a_moved_repository_only_with_the_same_contract(env, worker, change, same_contract):
    # The branch moved between the requirements session's clone and this session's.
    change.contract_commit = "0" * 40
    worker.repository_contract = (
        change.contract if same_contract else {**change.contract, "requirements": []})
    handle = env.client.get_workflow_handle(await service.start_session("requirements-session-" + "0" * 32))
    if same_contract:
        state = await wait_for_status(handle, "interviewing")
        assert state.contract_commit == "1" * 40 and worker.turns == 1
        await handle.terminate()
        return
    assert await handle.result() is None
    state = await handle.query(ApiDesignWorkflow.get_state)
    assert state.status == "failed" and "contract is no longer the one" in state.error
    assert worker.turns == 0 and state.messages == []


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
    # The contract the authors worked from is published with their sources.
    built = state.implementation["context"]
    assert worker.contract == {
        "language": built["language"], "requirements": built["requirements"],
        "requirements_document": built["requirements_document"], "api": built["api_design"]}
    assert len(worker.verified_images) == 1 and len(worker.checkouts) == 1
    with pytest.raises(sessions.SessionBusy):
        await service.retry_generation(handle.id, "repository")


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


async def test_failed_draft_falls_back_to_opening_question(env, worker):
    worker.fail_draft = True
    handle = await start(env)
    state = await wait_for_status(handle, "interviewing")
    assert state.messages == [{"role": "assistant", "content": OPENING_FALLBACK,
                               "questions": OPENING_QUESTIONS}]
    assert state.error is None
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(ApiDesignWorkflow.finalize)
    # Its questions are answered, as the user's next message.
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(ApiDesignWorkflow.send_message, "/rooms")
    await handle.execute_update(ApiDesignWorkflow.answer_questions, ["Mobile apps", "/rooms"])
    state = await handle.query(ApiDesignWorkflow.get_state)
    assert state.messages[1] == {
        "role": "user", "content": "Who will call the API?\n→ Mobile apps\n\n"
                                   "How should callers authenticate?\n→ /rooms"}
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
