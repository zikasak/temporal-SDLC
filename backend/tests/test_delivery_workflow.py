"""The delivery pipeline, entered directly and through the requirements session that
delivers a bugfix or a documentation task."""
import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.api.routes.requirements import router
from app.api_design import service as api_design_service
from app.api_design.models import empty_api
from app.delivery import service as delivery_service
from app.delivery.models import (
    STOPPED, DeliveryProgress, DeliveryState, TaskSpec, delivery_id,
)
from app.delivery.workflows import DeliveryWorkflow
from app.requirements import service as requirements_service
from app.requirements.document import render_requirements_markdown
from app.requirements.models import RequirementsState
from app.temporal import sessions
from app.temporal.executions import ensure_search_attributes
from app.temporal.workflows import ApiDesignWorkflow, RequirementsWorkflow
from tests.pipeline import (
    ADDED_TESTS, BASELINE_IMAGE, BROKEN_TEST, CODE, FakeCodegenWorkflow, FakeStages,
    finished, stage_workers,
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

    @activity.defn(name="register_session")
    async def register_session(self, session_id: str) -> None:
        pass

    @activity.defn(name="update_repository_checkout")
    async def update_repository_checkout(self, session_id: str,
                                         repository: dict[str, Any]) -> dict[str, Any]:
        if self.fail_checkout:
            raise ApplicationError("clone rejected", non_retryable=True)
        # The branch's latest commit holds the contract.
        self.has_contract = True
        return {"commit": "2" * 40}

    @activity.defn(name="read_repository_contract")
    async def read_repository_contract(self, session_id: str) -> dict[str, Any]:
        if not self.has_contract:
            raise ApplicationError(NO_CONTRACT, type="ContractError", non_retryable=True)
        return CONTRACT

    @activity.defn(name="api_design_turn")
    async def api_design_turn(self, state: Any) -> dict[str, Any]:
        return {"reply": "Draft ready.", "api": API, "language": "java"}

    @activity.defn(name="write_openapi_document")
    async def write_openapi_document(self, session_id: str, api: dict[str, Any]
                                     ) -> dict[str, Any]:
        return DOCUMENT


@pytest.fixture(scope="session")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@pytest.fixture
async def worker(env, monkeypatch):
    async with workers(env, monkeypatch) as activities:
        yield activities


@pytest.fixture(scope="module")
async def server():
    """The dev server, for what the time-skipping one does not do: tell a parent that its
    child was terminated, and terminate children with their parent."""
    async with await WorkflowEnvironment.start_local() as server:
        # Children are started with the repository's search attributes.
        await ensure_search_attributes(server.client)
        yield server


@pytest.fixture
async def live(server, monkeypatch):
    async with workers(server, monkeypatch) as activities:
        yield activities


@asynccontextmanager
async def workers(env, monkeypatch):
    activities = FakeActivities()

    async def connect():
        return env.client

    for module in (requirements_service, api_design_service, delivery_service, sessions):
        monkeypatch.setattr(module, "connect_temporal", connect)
    async with Worker(
        env.client, task_queue=TASK_QUEUE,
        workflows=[RequirementsWorkflow, ApiDesignWorkflow, DeliveryWorkflow],
        activities=[activities.requirements_turn, activities.write_requirements_document,
                    activities.read_requirements_document, activities.read_repository_contract,
                    activities.register_session,
                    activities.checkout_repository, activities.update_repository_checkout,
                    activities.publish_repository, activities.publish_documentation,
                    activities.open_pull_request, activities.api_design_turn, activities.write_openapi_document],
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
    return await finished(env, session_id)


# Only a documentation task is pushed without a check of it.
@pytest.mark.parametrize("outcome", ["error", "not_implemented"])
async def test_unverified_sources_are_not_pushed(env, worker, outcome, repository_selection):
    worker.verification_result = outcome
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


async def test_a_run_that_could_not_complete_runs_once_more(env, worker):
    worker.verification_results = ['error', 'passed']
    state = await delivered(env, await deliver(env))
    assert state.verification['status'] == 'passed' and state.verification_status == 'completed'
    assert worker.verified_images == [ORIGINAL, ORIGINAL] and not worker.diagnosed


async def test_a_correction_that_did_not_pass_goes_on_when_retried(env, worker):
    finding = {'summary': 'Rooms are listed unsorted.', 'target': 'implementation',
               'reason': 'The service orders them.'}
    worker.review_findings = [[finding], []]
    # Verified, reviewed, corrected; then the corrected delivery cannot be verified twice.
    worker.verification_results = ['passed', 'error', 'error', 'passed']
    session_id = await deliver(env)
    state = await delivered(env, session_id)
    assert state.correction_failed and state.verification_status == 'error'
    assert state.verification_error == 'Verification failed after correcting the review findings.'
    assert [stage for stage, _ in worker.repairs] == ['implementation']

    await delivery_service.retry(session_id, 'review')
    state = await delivered(env, session_id)
    # Only verified again, not corrected again, and then reviewed again.
    assert [stage for stage, _ in worker.repairs] == ['implementation']
    assert len(worker.verified_images) == 4 and worker.verified_images[-1] == (REPAIRED[0], ORIGINAL[1])
    assert not state.correction_failed and state.verification_error is None
    assert state.verification_status == 'completed'
    assert [item['findings'] for item in state.reviews] == [[finding], []]


async def test_a_retried_correction_restores_repository_tests_the_service_passes_again(env, worker, repository_selection):
    finding = {'summary': 'Instructions are not limited.', 'target': 'implementation',
               'reason': 'The service checks them.'}
    worker.review_findings = [[finding], []]
    lists, books = "com.example.RoomsTest#lists", "com.example.RoomsTest#books"
    worker.rewritten_tests = [lists, books]
    # The corrected service passes one rewritten test again, as the repository held it.
    worker.passing_after_correction = [lists]
    session_id = await deliver(env, repository_selection, kind="api_change")
    state = await delivered(env, session_id)
    assert state.correction_failed and "still passes were changed or removed" in state.verification_error

    retried = await delivery_service.retry(session_id, 'review')
    # The page sees the correction going on, not its failure.
    assert not retried.correction_failed and retried.verification_status == 'repairing'
    state = await delivered(env, session_id)
    # Verifying the same suite again would fail the same way: the test author puts the
    # test back, told why; the implementation is not corrected again.
    assert [stage for stage, _ in worker.repairs] == ['implementation', 'tests']
    review = worker.repairs[-1][1]['review']
    assert review['restore'] == [lists] and lists in review['findings'][0]['summary']
    assert worker.existing_runs[-1] == ("repository-tests", [books])
    assert not state.correction_failed and state.verification_error is None
    assert state.verification_status == 'completed' and state.publication_status == 'completed'


async def test_a_retried_correction_tells_the_test_author_which_test_skipped_and_why(env, worker, repository_selection):
    finding = {'summary': 'Instructions are not limited.', 'target': 'implementation',
               'reason': 'The service checks them.'}
    worker.review_findings = [[finding], []]
    # The corrected delivery passes, but one of its tests skipped itself.
    worker.verification_results = ['passed', 'skipped', 'passed']
    session_id = await deliver(env, repository_selection, kind="api_change")
    state = await delivered(env, session_id)
    assert state.correction_failed
    assert 'RoomsTest#books (skip reason: GitHub is not configured)' in state.verification_error

    await delivery_service.retry(session_id, 'review')
    state = await delivered(env, session_id)
    # The same suite would skip the same test again: its author is told which one and why.
    assert [stage for stage, _ in worker.repairs] == ['implementation', 'tests']
    summary = worker.repairs[-1][1]['review']['findings'][0]['summary']
    assert 'RoomsTest#books (skip reason: GitHub is not configured)' in summary
    assert not state.correction_failed and state.verification_error is None
    assert state.publication_status == 'completed'


async def test_review_rounds_end_when_a_review_only_repeats_corrected_findings(env, worker, repository_selection):
    unsorted = {'summary': 'Rooms are listed unsorted.', 'target': 'implementation',
                'reason': 'The service orders them.', 'path': 'service/Rooms.java'}
    again = {**unsorted, 'summary': 'GET /rooms still does not sort.', 'repeats': 1}
    paged = {'summary': 'GET /rooms ignores the page size.', 'target': 'implementation',
             'reason': 'The service pages.'}
    worker.review_findings = [[unsorted], [again], [paged], []]
    session_id = await deliver(env, repository_selection)
    state = await delivered(env, session_id)
    # The repeated finding is not sent for another correction: it is left to the user.
    assert [stage for stage, _ in worker.repairs] == ['implementation']
    assert state.verification_status == 'findings_open' and state.open_findings
    assert [item['findings'] for item in state.reviews] == [[unsorted], [again]]
    # The second review knew what the first had corrected.
    assert 'previous_findings' not in worker.reviewed[0]
    assert worker.reviewed[1]['previous_findings'] == [
        {'number': 1, 'summary': unsorted['summary'], 'target': 'implementation',
         'path': 'service/Rooms.java'}]

    await delivery_service.instruct_findings(session_id, 'Sort by room number.')
    state = await delivered(env, session_id)
    # Every round of the instructed run reviews under the user's instructions, and knows
    # the findings corrected from them as well as those corrected since.
    assert [context['instructions'] for context in worker.reviewed[2:]] == [
        'Sort by room number.'] * 2
    assert [item['summary'] for item in worker.reviewed[3]['previous_findings']] == [
        again['summary'], paged['summary']]
    # The author of the next correction never sees the bookkeeping.
    assert worker.repairs[-1][1]['review']['findings'] == [
        {key: value for key, value in paged.items() if key != 'target'}]
    assert state.verification_status == 'completed' and not state.open_findings
    assert 'instructions' in state.reviews[2] and 'instructions' not in state.reviews[3]


async def test_only_a_failed_review_or_correction_is_retried(env, worker):
    session_id = await deliver(env)
    state = await delivered(env, session_id)
    assert state.verification_status == 'completed'
    with pytest.raises(delivery_service.SessionBusy):
        await delivery_service.retry(session_id, 'review')


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
        assert sum(isinstance(result, DeliveryProgress) for result in attempts) == 1
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


async def wait_until(handle, condition) -> RequirementsState:
    for _ in range(100):
        state = await handle.query(RequirementsWorkflow.get_state)
        if condition(state):
            return state
        await asyncio.sleep(0.05)
    raise AssertionError("the session never reached the expected state")


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
    progress = await delivered(env, handle.id)
    # The session stays open while its delivery does.
    assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
    assert (await handle.query(RequirementsWorkflow.get_state)).document == DOCUMENT
    return handle, progress


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
    # Such a task has no API design.
    assert response["api_design_session_id"] is None


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
    # or a run that could not complete at all, nor when it ran once more.
    worker.verification_results = [outcome, *([outcome] if outcome == 'error' else []), 'passed']
    handle, state = await fixed(env, repository_selection)
    assert state.baseline_status == 'reproduced'
    if outcome == 'skipped':
        assert state.verification['status'] == 'passed'
        assert 'Tests skipped in the run against the changed service: 1' in state.verification_error
    else:
        assert state.verification['status'] == 'error' and state.verification_error is None
    assert state.publication_status == 'skipped' and not worker.published
    # The run answers for the tests the change added.
    assert worker.verified_tracked == [ADDED_TESTS] * (2 if outcome == 'error' else 1)

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


class IgnoresRestore(FakeActivities):
    """A test author that keeps the repository's tests it rewrote, whatever it is told."""

    @property
    def rewritten_tests(self) -> list[str]:
        return self.__dict__['rewritten_tests']

    @rewritten_tests.setter
    def rewritten_tests(self, value: list[str]) -> None:
        pass


LISTS, BOOKS = "com.example.RoomsTest#lists", "com.example.RoomsTest#books"
KEPT = "still passes were changed or removed: com.example.RoomsTest#lists (1)"


async def test_api_change_has_the_repository_tests_its_service_still_passes_put_back(env, worker, repository_selection):
    worker.rewritten_tests, worker.still_passing = [LISTS, BOOKS], [LISTS]
    state = await delivered(env, await deliver(env, repository_selection, kind="api_change"))
    # The test author is told which test the changed service still passes and puts it
    # back as the repository holds it; only the other rewritten test is run again.
    assert [stage for stage, _ in worker.repairs] == ["tests"]
    review = worker.repairs[0][1]["review"]
    assert review["restore"] == [LISTS] and LISTS in review["findings"][0]["summary"]
    assert worker.existing_runs == [("repository-tests", [LISTS, BOOKS]),
                                    ("repository-tests", [BOOKS])]
    # The restored suite goes on to the code review and is published.
    assert len(state.reviews) == 1 and state.verification_error is None
    assert state.verification_status == "completed" and state.publication_status == "completed"


async def test_a_suite_still_keeping_tests_after_the_restore_rounds_is_retried_with_the_restore(env, worker, repository_selection):
    worker.rewritten_tests, worker.still_passing = [LISTS, BOOKS], [LISTS]
    worker.__class__ = IgnoresRestore
    session_id = await deliver(env, repository_selection, kind="api_change")
    state = await delivered(env, session_id)
    # Two restore rounds, each verified again, and the suite still rewrites the test.
    assert [stage for stage, _ in worker.repairs] == ["tests", "tests"]
    assert len(worker.existing_runs) == 3 and KEPT in state.verification_error
    assert state.publication_status == "skipped" and not worker.published and not state.reviews

    worker.__class__ = FakeActivities
    await delivery_service.retry(session_id, "tests")
    state = await delivered(env, session_id)
    # The retried author is not started blind: it gets the reason and what to put back.
    retried = worker.test_contexts[-1]["repair"]["review"]
    assert retried["restore"] == [LISTS] and LISTS in retried["findings"][0]["summary"]
    assert worker.existing_runs[-1] == ("repository-tests", [BOOKS])
    assert state.verification_error is None and state.publication_status == "completed"


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


async def test_documentation_is_written_alone_and_pushed_as_unverified(env, worker, repository_selection):
    worker.kind = "docs"
    worker.fail_documentation = True
    handle, state = await fixed(env, repository_selection)
    # Without documentation there is nothing to push.
    assert state.documentation_error and state.verification_status == "skipped"
    assert state.publication_status == "skipped" and not worker.published_documentation
    with pytest.raises(sessions.SessionBusy):
        await requirements_service.retry_generation(handle.id, "tests")

    worker.fail_documentation = False
    await requirements_service.retry_generation(handle.id, "documentation")
    state = await delivered(env, handle.id)
    # One author and nothing else: no code, no tests, no service image, no test run.
    assert worker.stages == ["documentation", "documentation"] and not worker.snapshots
    assert FakeCodegenWorkflow.languages == [] and state.baseline_status == "not_applicable"
    assert state.documentation_error is None
    assert state.documentation["changed_files"] == ["README.md"]
    # It works from the repository's contract and the interview's brief, whose
    # requirements keep their IDs: they do not join the contract.
    brief = {"requirements": (await handle.query(RequirementsWorkflow.get_state)).requirements,
             "requirements_document": "# Brief", "retired": []}
    assert [item["id"] for item in brief["requirements"]] == ["FR-1"]
    assert state.documentation["context"] == {
        "requirements": CONTRACT["requirements"], "api_design": API, "language": "java",
        "requirements_document": CONTRACT["requirements_document"], "brief": brief,
        "repository": repository_selection,
    }
    # Nothing checks documentation yet, and the result says so instead of passing.
    assert state.verification_status == "completed"
    assert state.verification["status"] == "not_implemented"
    # It is pushed all the same, marked as unverified, and the contract is left alone.
    assert state.publication_status == "completed" and state.publication["verified"] is False
    assert worker.published_documentation == [(handle.id, repository_selection["full_name"], False)]
    assert not worker.published

    response = await requirements_service.get_session(handle.id)
    assert response["delivery"]["publication"]["verified"] is False
    assert response["api_design_session_id"] is None


@pytest.mark.parametrize("kind", ["bugfix", "api_change", "docs"])
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


async def test_a_session_blocked_on_its_contract_goes_on_from_the_latest_commit(env, worker, repository_selection):
    worker.has_contract = False
    worker.kind = "docs"
    handle = await specify_bugfix(env, repository_selection)
    await handle.execute_update(RequirementsWorkflow.finalize)
    assert (await handle.query(RequirementsWorkflow.get_state)).error == NO_CONTRACT

    # A failed update keeps the working copy the session had, and says so.
    worker.fail_checkout = True
    await requirements_service.update_checkout(handle.id)
    state = await wait_until(handle, lambda state: state.status == "interviewing")
    assert state.checkout == {"commit": "1" * 40} and "checked out again" in state.error

    worker.fail_checkout = False
    await requirements_service.update_checkout(handle.id)
    state = await wait_until(handle, lambda state: state.checkout["commit"] == "2" * 40)
    assert state.status == "interviewing" and state.error is None
    await handle.execute_update(RequirementsWorkflow.finalize)
    state = await handle.query(RequirementsWorkflow.get_state)
    assert state.status == "completed" and state.contract_commit == "2" * 40
    await delivered(env, handle.id)
    # Nothing can change what a finished session was finalized against.
    with pytest.raises(sessions.SessionBusy):
        await requirements_service.update_checkout(handle.id)


async def test_api_change_keeps_the_contract_for_its_design_and_delivers_nothing(env, worker, repository_selection):
    worker.kind = "api_change"
    handle = await specify_bugfix(env, repository_selection)
    await handle.execute_update(RequirementsWorkflow.finalize)
    state = await handle.query(RequirementsWorkflow.get_state)
    assert state.status == "completed" and state.document == DOCUMENT
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


async def test_agent_questions_wait_for_the_users_answers(env, worker):
    question = {"question": "Which database?", "options": ["Postgres", "MySQL"]}
    worker.implementation_questions = [question]
    session_id = await deliver(env)
    for _ in range(200):
        progress = await delivery_service.get_progress(session_id)
        if progress and progress.agent_questions:
            break
        await asyncio.sleep(0.05)
    asked, = progress.agent_questions
    assert asked["stage"] == "implementation" and asked["questions"] == [question]
    assert progress.verification_status == "waiting"

    # Answers reach only the agent that asked, and only when they answer every question.
    with pytest.raises(sessions.SessionBusy):
        await delivery_service.answer(session_id, "implementation-other", ["Postgres"])
    # The time-skipping server reports a rejected update only to its caller's result.
    agent = env.client.get_workflow_handle(asked["workflow_id"])
    with pytest.raises(WorkflowUpdateFailedError):
        await agent.execute_update("answer_questions", ["Postgres", "MySQL"])
    await delivery_service.answer(session_id, asked["workflow_id"], [" A managed Postgres "])

    progress = await delivered(env, session_id)
    assert progress.agent_questions == [] and progress.verification["status"] == "passed"
    first, resumed = worker.implementation_contexts
    assert "answers" not in first and resumed["answers"] == ["A managed Postgres"]
    assert all("answers" not in context for context in worker.test_contexts)
    with pytest.raises(sessions.SessionBusy, match="no longer open"):
        await delivery_service.answer(session_id, asked["workflow_id"], ["MySQL"])


async def test_a_task_is_one_workflow_tree_open_until_its_delivery_closes(server, live, repository_selection):
    """Requirements → API design → delivery, retries included, stay under one root, so no
    part of a task falls to the namespace's retention while another goes on."""
    live.kind = "new_service"
    live.fail_tests = True
    requirements = await specify_bugfix(server, repository_selection)
    await requirements.execute_update(RequirementsWorkflow.finalize)
    # The session starts the design itself once the document is written.
    state = await wait_until(requirements, lambda state: state.api_design_session_id)
    design = server.client.get_workflow_handle(state.api_design_session_id)
    assert (await design.describe()).parent_id == requirements.id
    for _ in range(100):
        if (await design.query(ApiDesignWorkflow.get_state)).status == "interviewing":
            break
        await asyncio.sleep(0.05)
    await design.execute_update(ApiDesignWorkflow.finalize)
    progress = await delivered(server, design.id)
    assert progress.tests_error and progress.verification_status == "skipped"

    # The retry continues the delivery as new, still the design's child.
    first_run = (await delivery(server, design.id).describe()).run_id
    live.fail_tests = False
    await delivery_service.retry(design.id, "tests")
    progress = await delivered(server, design.id)
    assert progress.tests_error is None and progress.verification["status"] == "passed"
    retried = await delivery(server, design.id).describe()
    assert retried.run_id != first_run and retried.parent_id == design.id
    for handle in (requirements, design, delivery(server, design.id)):
        assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
    await requirements.terminate()

async def test_a_stopped_delivery_is_retried_from_the_stage_it_stopped_in(server, live, repository_selection):
    """Terminating a stuck delivery leaves its session open, the stage it was in failed."""
    live.release_tests.clear()
    handle = await specify_bugfix(server, repository_selection)
    await handle.execute_update(RequirementsWorkflow.finalize)
    await asyncio.wait_for(live.tests_started.wait(), timeout=10)
    await delivery(server, handle.id).terminate()
    progress = await delivery_service.get_progress(handle.id)
    assert progress.tests_error == STOPPED and progress.verification_status == "error"
    assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING

    live.release_tests.set()
    await delivery_service.retry(handle.id, "tests")
    progress = await delivered(server, handle.id)
    assert progress.tests_error is None and progress.verification["status"] == "passed"
    assert (await delivery(server, handle.id).describe()).parent_id == handle.id
    await handle.terminate()
    # The root takes the whole task with it.
    for _ in range(100):
        if (await delivery(server, handle.id).describe()).status != WorkflowExecutionStatus.RUNNING:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("the delivery outlived its session")


def delivery(env, session_id: str) -> Any:
    return env.client.get_workflow_handle(delivery_id(session_id))


async def test_only_the_first_of_answers_sent_together_resumes_the_agent():
    """Updates delivered in one workflow task are all validated before any handler runs."""
    from app.implementation.workflows import ImplementationWorkflow

    agent = ImplementationWorkflow()
    agent._questions = [{"question": "Which database?", "options": ["Postgres", "MySQL"]}]
    agent.validate_answer_questions(["Postgres"])
    agent.validate_answer_questions(["MySQL"])
    await agent.answer_questions(["Postgres"])
    with pytest.raises(ApplicationError, match="not waiting"):
        await agent.answer_questions(["MySQL"])
    assert agent._answers == ["Postgres"]
