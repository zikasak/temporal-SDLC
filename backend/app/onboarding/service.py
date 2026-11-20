"""Onboardings: one long-running Temporal workflow per repository branch onboarded, the
onboarding interview stage of a top-level task flow (TaskWorkflow with origin onboarding)
that also holds its requirements review and delivery."""

import asyncio
import dataclasses
import logging
import uuid
from typing import Any

from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app.config import TASK_QUEUE
from app.delivery import service as delivery_service
from app.delivery.models import DeliveryProgress, RetryStage
from app.github.models import RepositorySelection
from app.onboarding import storage
from app.architecture.models import normalize_structure, render_architecture_markdown
from app.onboarding.models import (
    FINISHED_STATUSES, OnboardingState, draft_kind_changed, drafted_architecture_only,
    onboarding_problems,
)
from app.requirements.lookup import checkout_requirements, lookup_requirements
from app.requirements_review.models import forked_review, review_id, review_view
from app.tasks.models import FORK_SUFFIX, TaskFlowState, bounded_name, new_task_flow_id
from app.tasks.service import derived_name, start_task_flow, task_queue_of
from app.temporal.client import connect_temporal
from app.temporal.registry import register
from app.temporal.sessions import SessionBusy, fork_messages, start_update, state_at_message

logger = logging.getLogger(__name__)

WORKFLOW_TYPE = "OnboardingWorkflow"
TURN_ACTIVITY = "onboarding_turn"
ANOTHER_ONBOARDING = ("Another onboarding of this repository and branch is unfinished. "
                      "Finish it or continue it instead.")


def new_session_id() -> str:
    return f"onboarding-session-{uuid.uuid4().hex}"


async def get_state(session_id: str) -> OnboardingState:
    client = await connect_temporal()
    handle = client.get_workflow_handle(session_id)
    state = await handle.query("get_state", result_type=OnboardingState)
    if state.status not in FINISHED_STATUSES:
        # An onboarding whose execution closed without finishing, such as an old-shape one
        # cancelled at startup, keeps its last status; it is over all the same.
        description = await handle.describe()
        if description.status not in (None, WorkflowExecutionStatus.RUNNING):
            state.status = "failed"
            state.error = state.error or (
                OLD_SHAPE_REASON if description.status == WorkflowExecutionStatus.CANCELED
                else "The onboarding ended before it finished; start it again.")
    return state


async def _unfinished(session_id: str) -> bool:
    try:
        state = await get_state(session_id)
    except RPCError as exc:
        # An onboarding whose workflow is gone has ended long ago.
        if exc.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return state.status not in FINISHED_STATUSES


def onboarding_name(repository: RepositorySelection) -> str:
    """The name an onboarding's task flow is listed with."""
    return bounded_name(f"Onboarding {repository['full_name']} ({repository['branch']})")


async def _start(session_id: str, state: OnboardingState, *, name: str = "",
                 origin: str = "new", task_queue: str | None = None) -> str | None:
    """Start a new top-level task flow whose onboarding interview `state` is, with its
    requirements review and delivery pending; returns the task flow's ID."""
    repository = state.repository
    assert repository is not None  # an onboarding is of a repository
    task_flow_id = new_task_flow_id(uuid.uuid4().hex)
    state.task_flow_id = task_flow_id
    state.review_id = review_id(session_id)
    client = await connect_temporal()
    started = await start_task_flow(task_flow_id, TaskFlowState(
        repository=repository, name=name or onboarding_name(repository),
        origin=origin,  # type: ignore[arg-type]
        source="onboarding",
        onboarding_session_id=session_id, review_session_id=state.review_id,
        delivery_session_id=session_id, onboarding=state,
    ), client, task_queue or TASK_QUEUE, session_id)
    await register(session_id)
    return started


async def _discard(task_flow_id: str | None, session_id: str) -> None:
    """Terminate a fork, with its task flow, whose original could not be superseded."""
    client = await connect_temporal()
    for workflow_id in (task_flow_id, session_id):
        if not workflow_id:
            continue
        try:
            await client.get_workflow_handle(workflow_id).terminate(
                reason="The original onboarding could not be superseded.")
        except Exception:  # noqa: BLE001 - the original error is what the caller reports
            logger.warning("Could not terminate %s of the fork", workflow_id, exc_info=True)


async def start_onboarding(repository: RepositorySelection) -> str:
    """create_onboarding, returning the onboarding's ID alone."""
    return (await create_onboarding(repository))["session_id"]


async def create_onboarding(repository: RepositorySelection) -> dict[str, str]:
    """Start onboarding the repository's branch in a new task flow; SessionBusy while
    another onboarding of the same branch is unfinished. Returns the onboarding's and the
    task flow's IDs."""
    full_name, branch = repository["full_name"], repository["branch"]
    session_id = new_session_id()
    async with storage.branch_lock(full_name, branch) as conn:
        for other in await storage.sessions_of(conn, full_name, branch):
            if await _unfinished(other):
                raise SessionBusy(ANOTHER_ONBOARDING)
        task_flow_id = await _start(session_id, OnboardingState(repository=repository))
        await storage.add(conn, session_id, full_name, branch)
    logger.info("Started onboarding %s of %s@%s in task flow %s", session_id, full_name,
                branch, task_flow_id)
    return {"session_id": session_id, **({"task_flow_id": task_flow_id} if task_flow_id else {})}


async def fork_onboarding(session_id: str, message_index: int) -> str:
    """fork, returning the new onboarding's ID alone."""
    return (await fork(session_id, message_index))["session_id"]


async def fork(session_id: str, message_index: int) -> dict[str, str]:
    """A new onboarding of the same branch with the conversation, drafts, contract
    decision and review state up to the message, in a new task flow of its own; the
    original ends as superseded, and its task flow closes with it."""
    source = await get_state(session_id)
    if source.status in FINISHED_STATUSES:
        raise SessionBusy("Only an unfinished onboarding can be forked.")
    reviewing = source.review is not None and source.review.status == "running"
    if source.status not in ("interviewing", "awaiting_confirmation") and not reviewing:
        raise SessionBusy("Wait for the running turn, drafting or checkout update to "
                          "finish before forking.")
    messages = fork_messages(source.messages, message_index)
    drafts = {}
    for key in ("requirements", "api", "language", "architecture_structure", "conflicts"):
        drafts[key] = await state_at_message(
            session_id, source.messages, message_index,
            input_type=OnboardingState, default_input=OnboardingState(),
            activity=TURN_ACTIVITY, key=key,
        )
    repository = source.repository
    assert repository is not None  # an onboarding is of a repository
    fork_id = new_session_id()
    fork = OnboardingState(
        repository=repository,
        messages=messages,
        requirements=drafts["requirements"],
        api=drafts["api"],
        language=drafts["language"],
        forked_from={"session_id": session_id, "message_index": message_index},
        existing_artifacts=list(source.existing_artifacts),
        replaced_contract=source.replaced_contract,
        drafted=True,
        architecture_missing=source.architecture_missing,
        contract_problem=source.contract_problem,
        architecture_only=source.architecture_only,
        drafted_architecture_only=drafted_architecture_only(source),
        architecture_structure=drafts["architecture_structure"],
        conflicts=list(drafts["conflicts"] or []),
        # The review continues from the round the original had reached.
        review=forked_review(source.review, messages),
    )
    client = await connect_temporal()
    name = await derived_name(source.task_flow_id, onboarding_name(repository), FORK_SUFFIX,
                              client)
    async with storage.branch_lock(repository["full_name"], repository["branch"]) as conn:
        # The original ends only once the fork that replaces it exists, so that a failed
        # fork never loses the original; only the fork stays unfinished.
        task_flow_id = await _start(fork_id, fork, name=name, origin="fork",
                                    task_queue=await task_queue_of(session_id, client))
        try:
            await storage.add(conn, fork_id, repository["full_name"], repository["branch"])
            await start_update(session_id, "supersede")
        except BaseException:
            # The original stays as it was; the fork must not stay unfinished next to it.
            # Leaving the lock's transaction with the error also drops the fork's record.
            await _discard(task_flow_id, fork_id)
            raise
    logger.info("Forked onboarding %s after message %d as %s in task flow %s", session_id,
                message_index, fork_id, task_flow_id)
    return {"session_id": fork_id, **({"task_flow_id": task_flow_id} if task_flow_id else {})}


async def send_message(session_id: str, message: str) -> None:
    await start_update(session_id, "send_message", message)
    logger.info("Sent message to onboarding %s", session_id)


async def answer_questions(session_id: str, answers: list[str]) -> None:
    await start_update(session_id, "answer_questions", answers)
    logger.info("Sent answers to onboarding %s", session_id)


async def retry_message(session_id: str) -> None:
    await start_update(session_id, "retry_message")
    logger.info("Retrying the last turn of onboarding %s", session_id)


async def update_checkout(session_id: str) -> None:
    await start_update(session_id, "update_checkout")
    logger.info("Updating the checkout of onboarding %s", session_id)


async def decide_contract(session_id: str, decision: str) -> str:
    """Record the user's choice (replace, decline or architecture_only); returns the
    onboarding's status it leads to."""
    state = await get_state(session_id)
    await start_update(session_id, "contract_decision", decision)
    logger.info("Onboarding %s: decision about the existing contract: %s", session_id,
                decision)
    if decision == "decline":
        return "declined"
    # Drafts of the other kind than the decision asks for are discarded and drafted anew.
    kept = state.drafted and not draft_kind_changed(state, decision == "architecture_only")
    return "interviewing" if kept else "drafting"


async def finalize(session_id: str, force: bool = False) -> None:
    """Finalize the onboarding; with `force` despite its requirements review."""
    await start_update(session_id, "finalize", force)
    logger.info("Finalizing onboarding %s%s", session_id, " with force" if force else "")


async def retry_review(session_id: str) -> None:
    await start_update(session_id, "retry_review")
    logger.info("Retrying the failed review round of onboarding %s", session_id)


def _delivers(state: OnboardingState) -> bool:
    return state.status == "completed"


async def answer_delivery_questions(session_id: str, workflow_id: str,
                                    answers: list[str]) -> None:
    if not _delivers(await get_state(session_id)):
        raise SessionBusy("This onboarding's delivery asked no questions.")
    await delivery_service.answer(session_id, workflow_id, answers)


async def retry_generation(session_id: str, stage: RetryStage) -> DeliveryProgress:
    if not _delivers(await get_state(session_id)):
        raise SessionBusy("Only failed generation can be retried.")
    return await delivery_service.retry(session_id, stage)


async def accept_findings(session_id: str, login: str) -> DeliveryProgress:
    if not _delivers(await get_state(session_id)):
        raise SessionBusy("The delivery has no open review findings.")
    return await delivery_service.accept_findings(session_id, login)


async def instruct_findings(session_id: str, instructions: str) -> DeliveryProgress:
    if not _delivers(await get_state(session_id)):
        raise SessionBusy("The delivery has no open review findings.")
    return await delivery_service.instruct_findings(session_id, instructions)


async def get_session(session_id: str) -> dict[str, Any]:
    state = await get_state(session_id)
    session = dataclasses.asdict(state)
    for key in ("drafted", "drafted_architecture_only", "artifacts_unknown",
                "contract_problem", "conflicts", "review_id"):
        del session[key]
    session["review"] = review_view(state.review)
    delivery = None
    if _delivers(state):
        delivered = await delivery_service.get_state(session_id)
        if (delivered and state.architecture_only and delivered.spec.architecture_only
                and delivered.spec.architecture_structure):
            # Fix rounds requested on the pull request changed the pushed architecture.
            session["architecture_structure"] = delivered.spec.architecture_structure
        delivery = dataclasses.asdict(
            delivered.progress if delivered
            else DeliveryProgress(verification_status=(
                "not_applicable" if state.architecture_only else "waiting")))
    architecture = None
    if session["architecture_structure"]:
        try:
            architecture = render_architecture_markdown(
                normalize_structure(session["architecture_structure"]))
        except (TypeError, ValueError, KeyError):
            logger.warning("Onboarding %s has a malformed architecture", session_id)
    return {"session_id": session_id, **session,
            "existing_contract": bool(state.existing_artifacts),
            "architecture": architecture,
            "problems": onboarding_problems(state), "delivery": delivery,
            # The drafted requirements and the existing contract's; none is retired.
            "lookup_requirements": lookup_requirements(
                state.requirements, await checkout_requirements(session_id))}


# The query of the onboardings from before they ran under task flows that still run.
OLD_SHAPE_QUERY = ("WorkflowType = 'OnboardingWorkflow' AND ExecutionStatus = 'Running' "
                   "AND ParentWorkflowId IS NULL")
OLD_SHAPE_REASON = ("Onboardings now run under task flows; this onboarding from before was "
                    "cancelled and has to be started again.")
# How often, and how far apart, the startup step tries while Temporal is not reachable.
OLD_SHAPE_ATTEMPTS = 10
OLD_SHAPE_RETRY_SECONDS = 60


async def cancel_old_shape_onboardings() -> int:
    """Cancel every still-running onboarding started before onboardings ran under task
    flows (a top-level OnboardingWorkflow), with its delivery and code generation; they
    stay listed as cancelled and their sessions readable. Idempotent: a later call finds
    none. Returns how many were cancelled."""
    client = await connect_temporal()
    cancelled = 0
    async for item in client.list_workflows(OLD_SHAPE_QUERY):
        try:
            await client.get_workflow_handle(item.id, run_id=item.run_id).cancel()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            continue
        cancelled += 1
        logger.info("Cancelled the old-shape onboarding %s", item.id)
    return cancelled


async def cancel_old_shape_onboardings_loop() -> None:
    """Run cancel_old_shape_onboardings once at startup, trying again while Temporal is
    not reachable; it never keeps the API from serving."""
    for _ in range(OLD_SHAPE_ATTEMPTS):
        try:
            cancelled = await cancel_old_shape_onboardings()
        except Exception as exc:  # noqa: BLE001 - tried again later
            logger.warning("Old-shape onboardings not cancelled yet: %s", exc)
            await asyncio.sleep(OLD_SHAPE_RETRY_SECONDS)
            continue
        if cancelled:
            logger.info("Cancelled %d onboardings started before task flows", cancelled)
        return
