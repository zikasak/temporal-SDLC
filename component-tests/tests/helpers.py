"""Fakes and helpers shared by the component tests.

The fakes here stand for collaborators the stubs name: the workflows the code under test
starts or reaches by their registered names ("DeliveryWorkflow", "RequirementsWorkflow",
"ArchitectureWorkflow", "ApiDesignWorkflow"). Nothing here replaces a seam itself.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence, TypeVar

import pytest
from temporalio import workflow
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError, WorkflowHandle
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from app.api_design.models import ApiDesignState
from app.architecture.models import ArchitectureResult, ArchitectureState
from app.delivery.models import DeliveryProgress, DeliveryState, TaskSpec
from app.requirements.models import RequirementsState

REPOSITORY: dict[str, Any] = {
    "provider": "github",
    "repository_id": 1402961137,
    "full_name": "acme/widgets",
    "html_url": "https://github.com/acme/widgets",
    "private": True,
    "branch": "main",
}
CHECKOUT: dict[str, Any] = {"commit": "0123456789abcdef0123456789abcdef01234567"}

HOUR = timedelta(hours=1)
T = TypeVar("T")


def unique_queue() -> str:
    return f"tq-{uuid.uuid4()}"


def make_worker(
    client: Client, queue: str, workflows: Sequence[type],
    activities: Sequence[Callable[..., Any]] = (),
) -> Worker:
    return Worker(
        client, task_queue=queue, workflows=list(workflows), activities=list(activities),
        workflow_runner=UnsandboxedWorkflowRunner(),
    )


# ---------------------------------------------------------------- bounded waiting


async def wait_until(condition: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
    """Poll `condition` for at most `timeout` seconds; fail the test saying what did not happen."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            pytest.fail(f"timed out: {what}")
        await asyncio.sleep(0.05)


async def bounded(awaitable: Awaitable[T], what: str, timeout: float = 10.0) -> T:
    """Await `awaitable` for at most `timeout` seconds; fail the test saying what did not happen."""
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError:
        pytest.fail(f"timed out: {what}")


async def count_executions(client: Client, workflow_type: str, expected: int, timeout: float = 5.0) -> int:
    """Count the executions of a workflow type in the visibility store, waiting (bounded)
    until at least `expected` are listed; returns the count then."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        count = len([w async for w in client.list_workflows(f'WorkflowType = "{workflow_type}"')])
        if count >= expected or loop.time() > deadline:
            return count
        await asyncio.sleep(0.1)


async def wait_closed(
    handle: WorkflowHandle[Any, Any], what: str, timeout: float = 5.0,
) -> WorkflowExecutionStatus:
    """Wait (bounded) for the execution to close and return its final status."""
    try:
        await asyncio.wait_for(handle.result(), timeout=timeout)
    except asyncio.TimeoutError:
        desc = await handle.describe()
        pytest.fail(f"timed out: {what} (execution status {desc.status})")
    except WorkflowFailureError:
        pass
    desc = await handle.describe()
    assert desc.status is not None
    return desc.status


@asynccontextmanager
async def in_background(awaitable: Awaitable[T]) -> AsyncIterator["asyncio.Future[T]"]:
    """Run `awaitable` as a task while the body runs; a task still pending at the end is
    cancelled (the code under test may wait for a delivery on a task queue the test does not serve)."""
    task = asyncio.ensure_future(awaitable)
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def deliveries_besides(
    client: Client, known: Sequence[str], what: str, timeout: float = 8.0,
) -> list[Any]:
    """Wait (bounded) until the namespace lists a "DeliveryWorkflow" execution whose workflow ID
    is not in `known`, whatever task queue it was started on; returns those executions."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        found = [
            w async for w in client.list_workflows('WorkflowType = "DeliveryWorkflow"')
            if w.id not in known
        ]
        if found:
            return found
        if loop.time() > deadline:
            pytest.fail(f"timed out: {what}")
        await asyncio.sleep(0.1)


async def started_with(client: Client, workflow_id: str) -> DeliveryState:
    """The DeliveryState a "DeliveryWorkflow" execution was started with, read from its history."""
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    attributes = history.events[0].workflow_execution_started_event_attributes
    [state] = await client.data_converter.decode(list(attributes.input.payloads), [DeliveryState])
    return state


async def wait_started(handle: WorkflowHandle[Any, Any], timeout: float = 5.0) -> None:
    """Wait until the first workflow task ran, that is until a query is answered."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            await handle.query("get_state")
            return
        except Exception:
            if loop.time() > deadline:
                pytest.fail("timed out: the workflow did not start")
            await asyncio.sleep(0.05)


# ---------------------------------------------------------------- state builders


def delivery_state(
    session_id: str = "sess-1", *, number: int = 7, onboarding: bool = False,
    open_findings: bool = False, record_only: bool = True, waiting: bool = True,
    language: str | None = None,
) -> DeliveryState:
    """The state a delivery waits with after its push: a publication and an open pull request."""
    publication = {
        "branch": f"delivery/{session_id}", "commit": CHECKOUT["commit"],
        "url": f"https://github.com/acme/widgets/tree/delivery/{session_id}",
        "ignored": [], "ignored_count": 0, "verified": not open_findings,
    }
    pull_request = {
        "number": number, "title": "Add the widget endpoint",
        "url": f"https://github.com/acme/widgets/pull/{number}",
        "draft": open_findings, "base": "main",
    }
    progress = DeliveryProgress(
        publication=publication, publication_status="completed",
        pull_request=pull_request, pull_request_status="completed",
        open_findings=open_findings,
    )
    spec = TaskSpec(
        session_id=session_id,
        requirements=[{"id": "R1", "title": "Widgets", "description": "Serve widgets"}],
        requirements_document="# Requirements\n\nServe widgets.\n",
        api={"paths": {}},
        kind="api_change",
        language=language,
        repository=dict(REPOSITORY),
        checkout=dict(CHECKOUT),
        onboarding=onboarding,
    )
    return DeliveryState(spec=spec, progress=progress, record_only=record_only, waiting=waiting)


def architecture_only_state(session_id: str = "onb-1") -> DeliveryState:
    """An architecture-only onboarding's delivery, about to push."""
    spec = TaskSpec(
        session_id=session_id, requirements=[], requirements_document="", api={"paths": {}},
        kind=None, repository=dict(REPOSITORY), checkout=dict(CHECKOUT), onboarding=True,
        architecture="# Architecture\n\nOne module.\n",
        architecture_structure={"components": [{"name": "core"}]},
        architecture_only=True,
    )
    return DeliveryState(spec=spec)


def mention_for(comment_id: int, number: int = 7) -> dict[str, Any]:
    return {
        "comment_id": comment_id, "comment_type": "conversation",
        "url": f"https://github.com/acme/widgets/pull/{number}#issuecomment-{comment_id}",
        "author": "alice", "body": "@flows-app please fix the typo in the README",
        "received_at": "2026-11-08T10:00:00Z", "classification": None, "status": "received",
        "repository": "acme/widgets", "pull_number": number, "reply_to": None,
    }


def requirements_state() -> RequirementsState:
    return RequirementsState(
        messages=[
            {"role": "user", "content": "I need a widget service."},
            {"role": "assistant", "content": "Anything else I should record?"},
        ],
        requirements=[{
            "id": "R1", "category": "functional", "priority": "must",
            "title": "Serve widgets", "description": "The service serves widgets.",
        }],
        kind="new_service", status="interviewing",
        repository=dict(REPOSITORY), checkout=dict(CHECKOUT),
    )


def api_design_state(source_session_id: str = "req-1", *, status: str = "completed") -> ApiDesignState:
    return ApiDesignState(
        source_session_id=source_session_id,
        requirements=[{"id": "R1", "title": "Serve widgets"}],
        requirements_document="# Requirements\n",
        kind="new_service",
        messages=[
            {"role": "assistant", "content": "Here is the API."},
            {"role": "user", "content": "Looks good."},
            {"role": "assistant", "content": "Finalized."},
        ],
        api={"paths": {"/widgets": {}}},
        language="python",
        status=status,
        document={"document_id": "openapi-doc-1"} if status == "completed" else None,
        repository=dict(REPOSITORY), checkout=dict(CHECKOUT),
    )


def architecture_state(source_session_id: str) -> ArchitectureState:
    return ArchitectureState(
        source_session_id=source_session_id, kind="new_service",
        requirements=[{"id": "R1", "title": "Serve widgets"}],
        requirements_document="# Requirements\n",
        messages=[
            {"role": "user", "content": "Keep it to one module."},
            {"role": "assistant", "content": "Drafted the architecture."},
        ],
        status="interviewing", repository=dict(REPOSITORY), checkout=dict(CHECKOUT),
    )


# ---------------------------------------------------------------- recording of the fakes


@dataclass
class Started:
    workflow_id: str
    parent: Any
    state: Any


@dataclass
class Recorder:
    """What the fake workflows saw; reset before each test by the `recorder` fixture."""
    delivery_runs: list[Started] = field(default_factory=list)
    restarts: list[Started] = field(default_factory=list)
    mentions: list[Started] = field(default_factory=list)
    recorded: dict[tuple[str, str, int], str] = field(default_factory=dict)
    child_runs: list[Started] = field(default_factory=list)

    def clear(self) -> None:
        self.delivery_runs.clear()
        self.restarts.clear()
        self.mentions.clear()
        self.recorded.clear()
        self.child_runs.clear()


RECORD = Recorder()


# ---------------------------------------------------------------- fake workflows


@workflow.defn(name="DeliveryWorkflow")
class FakeDelivery:
    """Stands for the "DeliveryWorkflow" that the session services reach by its name: it keeps
    the state it started or restarted with, answers `get_state`, takes the `restart` and
    `mention` updates, and closes when it gets the `finish` signal (or is cancelled)."""

    @workflow.init
    def __init__(self, state: DeliveryState) -> None:
        self.state = state
        self.done = False
        if not workflow.unsafe.is_replaying():
            info = workflow.info()
            RECORD.delivery_runs.append(Started(info.workflow_id, info.parent, state))

    @workflow.run
    async def run(self, state: DeliveryState) -> DeliveryProgress:
        await workflow.wait_condition(lambda: self.done)
        return self.state.progress

    @workflow.signal
    def finish(self) -> None:
        self.done = True

    @workflow.query
    def get_state(self) -> DeliveryState:
        return self.state

    @workflow.update
    def restart(self, state: DeliveryState) -> None:
        self.state = state
        if not workflow.unsafe.is_replaying():
            info = workflow.info()
            RECORD.restarts.append(Started(info.workflow_id, info.parent, state))

    @workflow.update
    def mention(self, mention: Any) -> None:
        if not workflow.unsafe.is_replaying():
            info = workflow.info()
            update = workflow.current_update_info()
            assert update is not None
            RECORD.mentions.append(Started(info.workflow_id, info.parent, mention))
            key = (self.state.spec.session_id, mention["comment_type"], mention["comment_id"])
            RECORD.recorded[key] = f"{info.run_id}:{update.id}"


@workflow.defn(name="RequirementsWorkflow")
class FakeRequirements:
    """Stands for a requirements session reached by its registered name."""

    @workflow.init
    def __init__(self, state: RequirementsState | None = None) -> None:
        self.state = state or RequirementsState()
        self.done = False

    @workflow.run
    async def run(self, state: RequirementsState | None = None) -> dict[str, Any] | None:
        await workflow.wait_condition(lambda: self.done)
        return None

    @workflow.signal
    def finish(self) -> None:
        self.done = True

    @workflow.query
    def get_state(self) -> RequirementsState:
        return self.state


@workflow.defn(name="ArchitectureWorkflow")
class FakeArchitecture:
    """Stands for an architecture session reached by its registered name."""

    @workflow.init
    def __init__(self, state: ArchitectureState) -> None:
        self.state = state
        self.done = False

    @workflow.run
    async def run(self, state: ArchitectureState) -> ArchitectureResult | None:
        await workflow.wait_condition(lambda: self.done)
        return None

    @workflow.signal
    def finish(self) -> None:
        self.done = True

    @workflow.query
    def get_state(self) -> ArchitectureState:
        return self.state


async def close_workflow(handle: WorkflowHandle[Any, Any], outcome: str) -> None:
    """Close a fake workflow as completed (its `finish` signal) or cancelled (a cancel request)."""
    if outcome == "completed":
        await handle.signal("finish")
        status = await wait_closed(handle, "the fake workflow did not complete")
        assert status == WorkflowExecutionStatus.COMPLETED
    else:
        await handle.cancel()
        status = await wait_closed(handle, "the fake workflow did not end cancelled")
        assert status == WorkflowExecutionStatus.CANCELED


def patch_everywhere(
    monkeypatch: pytest.MonkeyPatch, modules: Sequence[Any], name: str, replacement: Any,
) -> None:
    """Replace the attribute `name` in every module that has it (a module may import the
    collaborator by name or reach it through its own module)."""
    for module in modules:
        if hasattr(module, name):
            monkeypatch.setattr(module, name, replacement)


def connect_to(client: Client) -> Callable[[], Awaitable[Client]]:
    async def connect_temporal() -> Client:
        return client
    return connect_temporal
