"""The delivery pipeline, entered directly and through the requirements session that
delivers a bugfix."""
import asyncio
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.api.routes.requirements import router
from app.api_design import service as api_design_service
from app.api_design.models import empty_api
from app.delivery import service as delivery_service
from app.delivery.models import DeliveryProgress, DeliveryState, TaskSpec, delivery_id
from app.delivery.workflows import DeliveryWorkflow
from app.requirements import service as requirements_service
from app.requirements.document import render_requirements_markdown
from app.requirements.models import RequirementsState
from app.temporal import sessions
from app.temporal.workflows import RequirementsWorkflow
from tests.pipeline import (
    ADDED_TESTS, BASELINE_IMAGE, BROKEN_TEST, CODE, FakeCodegenWorkflow, FakeStages,
    stage_workers,
)

TASK_QUEUE = "delivery-test"
DOCUMENT = {"document_id": "0" * 32, "filename": "requirements.md",
            "content_type": "text/markdown", "size_bytes": 10, "revision": 1}
API = {**empty_api(), "endpoints": [{
    "method": "GET", "path": "/rooms", "summary": "S", "description": "", "tag": "",
    "parameters": [], "request_schema": None, "response_status": 200,
    "response_schema": None, "response_array": False, "error_statuses": [],
    "requirement_ids": [],
}]}
# What the repository was published with, as opposed to what the bugfix interview records.
PUBLISHED = [{"id": "FR-1", "category": "functional", "title": "Book rooms",
              "description": "Guests book rooms.", "priority": "must"}]
CONTRACT = {
    "language": "java", "api": API, "requirements": PUBLISHED,
    "requirements_document": render_requirements_markdown(PUBLISHED, "A hotel."),
}
NO_CONTRACT = "The repository has no readable contract in contracts/."
ORIGINAL = ("sha256:" + "a" * 64, "sha256:" + "b" * 64)
REPAIRED = ("sha256:" + "c" * 64, "sha256:" + "d" * 64)


class FakeActivities(FakeStages):
    def __init__(self) -> None:
        super().__init__()
        self.has_contract = True
        # The kind the user names in the interview.
        self.kind = "bugfix"
        # Requirements of the repository's contract the interview ends.
        self.retired: list[dict[str, Any]] = []

    @activity.defn(name="requirements_turn")
    async def requirements_turn(
        self, messages: list[dict[str, Any]], requirements: list[dict[str, Any]],
        kind: str | None, retired: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.turns += 1
        return {"reply": "Recorded.", "kind": self.kind, "retired": self.retired, "requirements": [{
            "id": "FR-1", "category": "functional", "title": messages[-1]["content"],
            "description": "D", "priority": "must",
        }]}

    @activity.defn(name="write_requirements_document")
    async def write_requirements_document(
        self, session_id: str, messages: list[dict[str, Any]],
        requirements: list[dict[str, Any]], retired: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.documented = requirements
        self.documented_retired = retired
        return DOCUMENT

    @activity.defn(name="read_requirements_document")
    async def read_requirements_document(self, session_id: str, document_id: str) -> str:
        return "# Brief"

    @activity.defn(name="read_repository_contract")
    async def read_repository_contract(self, session_id: str) -> dict[str, Any]:
        if not self.has_contract:
            raise ApplicationError(NO_CONTRACT, type="ContractError", non_retryable=True)
        return CONTRACT


@pytest.fixture(scope="session")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@pytest.fixture
async def worker(env, monkeypatch):
    activities = FakeActivities()

    async def connect():
        return env.client

    for module in (requirements_service, delivery_service, sessions):
        monkeypatch.setattr(module, "connect_temporal", connect)
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[RequirementsWorkflow, DeliveryWorkflow],
        activities=[activities.requirements_turn, activities.write_requirements_document,
                    activities.read_requirements_document, activities.read_repository_contract,
                    activities.checkout_repository, activities.publish_repository],
    ), stage_workers(env, activities):
        yield activities


async def deliver(env, repository=None, language: str | None = "java",
                  kind: str = "new_service"):
    """Start the delivery of a one-endpoint service, as a finished design does."""
    session_id = f"api-design-session-{uuid.uuid4().hex}"
    spec = TaskSpec(session_id=session_id, requirements=[],
                    requirements_document="# Requirements", api=API, document_id="0" * 32,
                    kind=kind, language=language, repository=repository,
                    checkout={"commit": "1" * 40} if repository else None)
    await env.client.start_workflow(DeliveryWorkflow.run, DeliveryState(spec=spec),
                                    id=delivery_id(session_id), task_queue=TASK_QUEUE)
    return session_id


async def delivered(env, session_id: str) -> DeliveryProgress:
    """The outcome of the session's delivery; it follows the latest manual retry."""
    return await env.client.get_workflow_handle(
        delivery_id(session_id), result_type=DeliveryProgress).result()


async def test_unverified_sources_are_not_pushed(env, worker, repository_selection):
    worker.verification_result = "error"
    state = await delivered(env, await deliver(env, repository_selection))
    assert state.publication_status == "skipped" and not worker.published


async def test_failed_tests_send_the_diagnosed_side_back_until_verification_passes(env, worker):
    worker.verification_results = ['failed', 'failed', 'passed']
    worker.diagnoses = ['tests', 'implementation']
    state = await delivered(env, await deliver(env))
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
    # The test author may change the tests the diagnosis found broken, and no others.
    assert tests_repair['broken_tests'] == state.repairs[0]['tests'] == [BROKEN_TEST]
    assert 'broken_tests' not in service_repair and state.repairs[1]['tests'] == []
    assert tests_repair['test_failures'][0]['name'] == 'lists'
    assert 'service_log' not in tests_repair
    assert service_repair['service_log'] == 'NullPointerException'
    # Each verification runs the images as they stand after the preceding repair.
    assert worker.verified_images == [ORIGINAL, (ORIGINAL[0], REPAIRED[1]), REPAIRED]
    assert (state.implementation['image_id'], state.tests['image_id']) == REPAIRED
    # Only a change to an existing service has a baseline to hold its tests against.
    assert state.baseline_status == 'not_applicable' and not worker.baselines


async def test_repair_stops_after_the_round_limit_with_the_last_failure(env, worker):
    worker.verification_result = 'failed'
    state = await delivered(env, await deliver(env))
    assert len(state.repairs) == len(worker.repairs) == 5
    assert len(worker.verified_images) == 6
    assert state.verification['status'] == 'failed' and state.verification_status == 'completed'
    assert state.verification_error == 'Tests still fail after 5 automatic repair rounds.'


@pytest.mark.parametrize('broken', ['diagnosis', 'implementation'])
async def test_failed_diagnosis_or_repair_keeps_the_published_images(env, worker, broken):
    worker.verification_results = ['failed', 'passed']
    worker.fail_diagnosis = broken == 'diagnosis'
    worker.release_verification.clear()
    session_id = await deliver(env)
    await asyncio.wait_for(worker.verification_started.wait(), timeout=10)
    # The first build succeeded; only the repair of it fails.
    worker.fail_implementation = broken == 'implementation'
    worker.release_verification.set()
    state = await delivered(env, session_id)
    assert len(worker.verified_images) == 1 and not worker.repairs
    assert state.verification['status'] == 'failed' and state.verification_status == 'completed'
    assert state.implementation['image_id'] == ORIGINAL[0]
    if broken == 'diagnosis':
        assert state.verification_error == 'The failed tests could not be diagnosed.'
        assert state.repairs == [] and state.implementation_error is None
        return
    assert state.implementation_error == 'The service could not be implemented or built.'

    # The failed repair is recoverable through the existing manual retry, which
    # rebuilds the service and resumes verification with a clean repair history.
    worker.fail_implementation = False
    await delivery_service.retry(session_id, 'server')
    state = await delivered(env, session_id)
    assert state.verification['status'] == 'passed' and state.repairs == []


@pytest.mark.parametrize('failed', [{'implementation'}, {'tests'}, {'implementation', 'tests'}])
async def test_failed_coding_branches_preserve_other_artifacts(env, worker, failed, repository_selection):
    worker.fail_implementation = 'implementation' in failed
    worker.fail_tests = 'tests' in failed
    session_id = await deliver(env, repository_selection)
    state = await delivered(env, session_id)
    assert state.code == CODE
    assert state.verification_status == 'skipped' and state.verification is None
    assert not worker.verification_started.is_set()
    if 'implementation' in failed:
        assert state.implementation is None
        assert state.implementation_error == "The service could not be implemented or built."
    else:
        assert state.implementation["image_id"] == ORIGINAL[0]
        assert state.implementation_error is None
    if 'tests' in failed:
        assert state.tests is None
        assert state.tests_error == "The service tests could not be generated or built."
    else:
        assert state.tests["image_id"] == ORIGINAL[1]
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
                await delivery_service.retry(session_id, 'tests' if sibling == 'tests' else 'server')
        # Another failure must remain retryable, including across closed runs.
        await delivery_service.retry(session_id, stage)
        still_failed = await delivered(env, session_id)
        assert getattr(still_failed, f'{branch}_error')
        assert still_failed.verification_status == 'skipped'
        assert not getattr(worker, f'{sibling}_started').is_set()
        getattr(worker, f'{branch}_started').clear()
        getattr(worker, f'release_{branch}').clear()
        setattr(worker, f'fail_{branch}', False)
        attempts = await asyncio.gather(
            delivery_service.retry(session_id, stage),
            delivery_service.retry(session_id, stage),
            return_exceptions=True,
        )
        assert sum(result is None for result in attempts) == 1
        assert sum(isinstance(result, sessions.SessionBusy) for result in attempts) == 1
        await asyncio.wait_for(getattr(worker, f'{branch}_started').wait(), timeout=10)
        with pytest.raises(sessions.SessionBusy):
            await delivery_service.retry(session_id, stage)
        pending = await delivery_service.get_progress(session_id)
        assert getattr(pending, f'{branch}_error') is None
        assert pending.verification_status == 'waiting'
        getattr(worker, f'release_{branch}').set()
        state = await delivered(env, session_id)
        assert getattr(state, branch)['image_id']
        assert getattr(state, f'{branch}_error') is None
        assert not getattr(worker, f'{sibling}_started').is_set()
        assert state.code == CODE
        # The retried stage still works in the session's repository.
        assert getattr(state, branch)['context']['repository'] == repository_selection
    assert state.verification_status == 'completed'
    assert worker.verification_started.is_set()


@pytest.mark.parametrize('first', ['tests', 'implementation'])
async def test_implementation_and_tests_start_before_either_finishes(env, worker, first):
    """Sequential children would leave one activity waiting forever at this barrier."""
    worker.release_implementation.clear()
    worker.release_tests.clear()
    worker.release_verification.clear()
    session_id = await deliver(env)
    try:
        await asyncio.wait_for(asyncio.gather(
            worker.implementation_started.wait(), worker.tests_started.wait(),
        ), timeout=10)
        state = await delivery_service.get_progress(session_id)
        assert state.implementation is None and state.tests is None
        assert state.verification_status == 'waiting'
        assert not worker.verification_started.is_set()
        # Either result is observable while the other branch remains running;
        # verification must not start until the second one succeeds.
        getattr(worker, f'release_{first}').set()
        for _ in range(100):
            state = await delivery_service.get_progress(session_id)
            if getattr(state, first) is not None:
                break
            await asyncio.sleep(0.05)
        assert getattr(state, first) is not None
        assert state.verification is None and not worker.verification_started.is_set()
        worker.release_implementation.set()
        worker.release_tests.set()
        await asyncio.wait_for(worker.verification_started.wait(), timeout=10)
        assert (await delivery_service.get_progress(session_id)).verification_status == 'running'
    finally:
        worker.release_implementation.set()
        worker.release_tests.set()
        worker.release_verification.set()
        await delivered(env, session_id)


async def test_failed_code_generation_can_be_retried(env, worker):
    FakeCodegenWorkflow.fail = True
    session_id = await deliver(env)
    state = await delivered(env, session_id)
    assert state.code is None
    assert state.code_error == "The server code could not be generated."
    assert not worker.implementation_started.is_set() and not worker.tests_started.is_set()
    assert state.verification_status == 'skipped'
    assert not worker.verification_started.is_set()

    with pytest.raises(sessions.SessionBusy):
        await delivery_service.retry(session_id, 'tests')
    FakeCodegenWorkflow.fail = False
    await delivery_service.retry(session_id, 'server')
    state = await delivered(env, session_id)
    assert state.code == CODE and state.code_error is None
    assert state.implementation and state.tests
    assert state.verification_status == 'completed'


async def specify_bugfix(env, repository) -> Any:
    """A requirements session in which the user described a bugfix; not yet finalized."""
    handle = await env.client.start_workflow(
        RequirementsWorkflow.run,
        RequirementsState(messages=[{"role": "assistant", "content": "Hello"}],
                          repository=repository),
        id=f"requirements-session-{uuid.uuid4().hex}", task_queue=TASK_QUEUE,
    )
    # No message is accepted until the repository is checked out.
    for _ in range(100):
        if (await handle.query(RequirementsWorkflow.get_state)).status == "interviewing":
            break
        await asyncio.sleep(0.05)
    await handle.execute_update(RequirementsWorkflow.send_message, "Rooms double-book")
    return handle


async def fixed(env, repository) -> tuple[Any, DeliveryProgress]:
    """The outcome of delivering a finalized bugfix session."""
    handle = await specify_bugfix(env, repository)
    await handle.execute_update(RequirementsWorkflow.finalize)
    # The session stays open until its delivery has ended.
    assert await handle.result() == DOCUMENT
    return handle, await delivered(env, handle.id)


async def test_bugfix_is_delivered_red_to_green_from_the_requirements_session(env, worker, repository_selection):
    handle, state = await fixed(env, repository_selection)
    # No code is generated, and nothing is fixed before the tests are seen to fail.
    assert FakeCodegenWorkflow.languages == [] and state.code is None
    assert worker.stages == ['tests', 'baseline', 'baseline-run', 'implementation', 'verification']
    assert state.baseline_status == 'reproduced' and state.baseline_error is None
    # The baseline runs the added tests against the service as the repository holds it.
    assert worker.baselines == [(BASELINE_IMAGE, ORIGINAL[1], ADDED_TESTS)]
    assert worker.verified_images == [ORIGINAL]
    # Both authors work from the repository's contract and the interview's brief.
    brief = {"requirements": (await handle.query(RequirementsWorkflow.get_state)).requirements,
             "requirements_document": "# Brief", "retired": []}
    # The repository's tests were recorded before either author ran.
    assert worker.snapshots == [[]]
    assert brief["requirements"][0]["title"] == "Rooms double-book"
    # The interview numbered it FR-1, which the repository's contract already has. It got
    # its own ID before anything quoted it: the document, the authors and the contract agree.
    assert [item["id"] for item in brief["requirements"]] == ["FR-2"]
    assert worker.documented == brief["requirements"]
    for built in (state.implementation["context"], state.tests["context"]):
        assert built == {
            "requirements": CONTRACT["requirements"], "api_design": API, "language": "java",
            "requirements_document": CONTRACT["requirements_document"], "brief": brief,
            "repository": repository_selection,
        }
    assert state.verification['status'] == 'passed' and state.publication_status == 'completed'
    assert worker.published == [(handle.id, repository_selection["full_name"])]
    # The API is as it was. What the fix required is now required of the service, so
    # that a later task keeps the tests written for it.
    required = [*PUBLISHED, *brief["requirements"]]
    assert worker.contract == {**CONTRACT, "requirements": required,
                               "requirements_document": render_requirements_markdown(required, "A hotel.")}
    assert worker.published_kind == "bugfix"

    # The session API reports the brief with its delivery, and not the contract.
    response = await requirements_service.get_session(handle.id)
    assert response["delivery"]["baseline_status"] == "reproduced"
    assert response["delivery"]["publication"]["branch"] == "generated/abc"
    assert "contract" not in response
    # Such a task has no API design to start.
    with pytest.raises(sessions.SessionBusy, match="no API design"):
        await api_design_service.start_session(handle.id)


@pytest.mark.parametrize('baseline, status, error', [
    ('not_reproduced', 'not_reproduced', 'com.example.RoomsTest#refusesDoubleBooking (1 of 2)'),
    ('existing_failed', 'error', 'Existing tests failing against the service as the repository holds it: 1'),
    ('skipped', 'error', 'Tests skipped in the baseline run: 1'),
    ('left_out', 'error', 'did not run in the baseline run: com.example.RoomsTest#lists (1)'),
    ('error', 'error', 'The baseline run could not complete.'),
])
async def test_nothing_is_fixed_unless_the_added_tests_fail_alone(env, worker, baseline, status, error, repository_selection):
    worker.baseline_results = [baseline, 'reproduced']
    handle, state = await fixed(env, repository_selection)
    assert state.baseline_status == status and error in state.baseline_error
    assert state.implementation is None and not worker.implementation_started.is_set()
    assert state.verification_status == 'skipped' and state.publication_status == 'skipped'
    assert not worker.published

    # Retrying the tests writes them again and holds them against the baseline again.
    with pytest.raises(sessions.SessionBusy):
        await requirements_service.retry_generation(handle.id, 'server')
    await requirements_service.retry_generation(handle.id, 'tests')
    state = await delivered(env, handle.id)
    assert worker.stages[-5:] == ['tests', 'baseline', 'baseline-run', 'implementation', 'verification']
    assert state.baseline_status == 'reproduced' and state.baseline_error is None
    assert state.verification['status'] == 'passed' and state.publication_status == 'completed'


@pytest.mark.parametrize('outcome', ['skipped', 'error'])
async def test_bugfix_is_not_published_until_every_test_is_seen_to_pass(env, worker, outcome, repository_selection):
    # Red on the unchanged service, then skipped rather than green on the changed one,
    # or a run that could not complete at all.
    worker.verification_results = [outcome, 'passed']
    handle, state = await fixed(env, repository_selection)
    assert state.baseline_status == 'reproduced'
    if outcome == 'skipped':
        assert state.verification['status'] == 'passed'
        assert 'Tests skipped in the run against the changed service: 1' in state.verification_error
    else:
        assert state.verification['status'] == 'error' and state.verification_error is None
    assert state.publication_status == 'skipped' and not worker.published
    # The run answers for the tests the change added.
    assert worker.verified_tracked == [ADDED_TESTS]

    await requirements_service.retry_generation(handle.id, 'tests')
    state = await delivered(env, handle.id)
    assert state.verification_error is None and state.publication_status == 'completed'


@pytest.mark.parametrize('still_red', [True, False])
async def test_repaired_tests_of_a_bugfix_must_still_fail_without_the_fix(env, worker, still_red, repository_selection):
    worker.verification_results = ['failed', 'passed']
    worker.diagnoses = ['tests']
    worker.baseline_results = ['reproduced', 'reproduced' if still_red else 'not_reproduced']
    # The repair corrects a repository test the fix made wrong: it fails without the fix too.
    worker.corrected_tests = ['com.example.RoomsTest#lists']
    _, state = await fixed(env, repository_selection)
    assert [stage for stage, _ in worker.repairs] == ['tests']
    assert [tracked for _, _, tracked in worker.baselines] == [
        ADDED_TESTS, [*ADDED_TESTS, *worker.corrected_tests]]
    # The repairing author gets the brief its tests are for.
    assert worker.diagnosed[0]['brief']['requirements_document'] == '# Brief'
    assert [tests for _, tests, _ in worker.baselines] == [ORIGINAL[1], REPAIRED[1]]
    if still_red:
        # Only the added tests have to fail there; the corrected one is not held against it.
        assert state.baseline_status == 'reproduced'
        assert state.verification['status'] == 'passed' and worker.published
        return
    # Added tests weakened until they pass without the fix prove nothing about it.
    assert state.baseline_status == 'not_reproduced'
    assert len(worker.verified_images) == 1 and state.verification['status'] == 'failed'
    assert state.publication_status == 'skipped' and not worker.published


async def test_bugfix_ends_the_requirements_its_interview_retired(env, worker, repository_selection):
    # The interviewer names a requirement of the contract and one that is not in it.
    worker.retired = [{**PUBLISHED[0], "title": "As the model remembers it"},
                      {**PUBLISHED[0], "id": "FR-9"}]
    handle, state = await fixed(env, repository_selection)
    added = (await handle.query(RequirementsWorkflow.get_state)).requirements
    # Both authors are told what ends, as the contract states it, and it leaves the
    # contract with the publication.
    assert state.tests["context"]["brief"]["retired"] == PUBLISHED
    assert worker.documented_retired == PUBLISHED
    assert worker.contract["requirements"] == added
    assert worker.contract["requirements_document"] == render_requirements_markdown(added, "A hotel.")


@pytest.mark.parametrize("still_passing, not_run, refused", [
    ([], [], None),
    (["com.example.RoomsTest#lists"], [],
     "still passes were changed or removed: com.example.RoomsTest#lists (1)"),
    # A rewritten test that was not seen to fail as it was is not shown to be unsupported.
    ([], ["com.example.RoomsTest#books"], "did not run, as they were, against the changed "
                                          "service: com.example.RoomsTest#books (1)"),
])
async def test_api_change_keeps_the_repository_tests_its_service_still_passes(env, worker, still_passing, not_run, refused, repository_selection):
    rewritten = ["com.example.RoomsTest#lists", "com.example.RoomsTest#books"]
    worker.rewritten_tests, worker.still_passing, worker.not_run = rewritten, still_passing, not_run
    session_id = await deliver(env, repository_selection, kind="api_change")
    state = await delivered(env, session_id)
    assert worker.snapshots == [[]]
    # Each rewritten test was run as the repository held it, against the changed service.
    assert worker.existing_runs == [("repository-tests", rewritten)]
    assert state.verification["status"] == "passed"
    if not refused:
        assert state.verification_error is None and state.publication_status == "completed"
        return
    assert refused in state.verification_error
    assert state.publication_status == "skipped" and not worker.published
    # The way on is a new suite, judged the same way.
    worker.still_passing = worker.not_run = []
    await delivery_service.retry(session_id, "tests")
    assert (await delivered(env, session_id)).publication_status == "completed"


async def test_api_change_is_not_published_with_a_test_that_did_not_run(env, worker, repository_selection):
    worker.verification_result = "skipped"
    state = await delivered(env, await deliver(env, repository_selection, kind="api_change"))
    assert "Tests skipped" in state.verification_error
    assert state.publication_status == "skipped" and not worker.published


async def test_nothing_is_authored_when_the_repository_tests_cannot_be_recorded(env, worker, repository_selection):
    worker.fail_snapshot = True
    session_id = await deliver(env, repository_selection)
    state = await delivered(env, session_id)
    assert state.code_error == "The repository's tests could not be recorded."
    assert not worker.stages and FakeCodegenWorkflow.languages == []
    assert state.verification_status == "skipped" and state.publication_status == "skipped"
    worker.fail_snapshot = False
    await delivery_service.retry(session_id, "server")
    assert (await delivered(env, session_id)).publication_status == "completed"


@pytest.mark.parametrize("kind", ["bugfix", "api_change"])
async def test_change_needs_a_repository_this_pipeline_published(env, worker, kind, repository_selection):
    worker.has_contract = False
    worker.kind = kind
    handle = await specify_bugfix(env, repository_selection)
    try:
        await handle.execute_update(RequirementsWorkflow.finalize)
        state = await handle.query(RequirementsWorkflow.get_state)
        # The user is told why, and can still change the kind of task.
        assert state.status == "interviewing" and state.error == NO_CONTRACT
        assert state.document is None and not worker.stages
        assert (await requirements_service.get_session(handle.id))["delivery"] is None
    finally:
        await handle.terminate()


async def test_api_change_keeps_the_contract_for_its_design_and_delivers_nothing(env, worker, repository_selection):
    worker.kind = "api_change"
    handle = await specify_bugfix(env, repository_selection)
    await handle.execute_update(RequirementsWorkflow.finalize)
    assert await handle.result() == DOCUMENT
    state = await handle.query(RequirementsWorkflow.get_state)
    # The design starts from this contract, and the change's requirements join it.
    assert state.contract == CONTRACT
    # The commit it was read at lets the design notice a branch that moved since.
    assert state.contract_commit == state.checkout["commit"] == "1" * 40
    assert [item["id"] for item in state.requirements] == ["FR-2"]
    assert worker.documented == state.requirements
    # The API design delivers the task, not this session.
    assert not worker.stages
    assert (await requirements_service.get_session(handle.id))["delivery"] is None
    with pytest.raises(sessions.SessionBusy):
        await requirements_service.retry_generation(handle.id, "tests")


async def test_generation_retry_http_validation(env, worker, repository_selection):
    app = FastAPI()
    app.include_router(router, prefix='/api')
    handle = await specify_bugfix(env, repository_selection)
    path = f'/api/requirements/sessions/{handle.id}/generation/retry'
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
            assert (await http.post(path, json={'stage': 'verification'})).status_code == 422
            # Nothing has been delivered yet.
            assert (await http.post(path, json={'stage': 'tests'})).status_code == 409
    finally:
        await handle.terminate()
