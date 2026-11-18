"""Onboardings: one long-running Temporal workflow per repository branch onboarded."""

import dataclasses
import logging
import uuid
from typing import Any

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
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import execution_fields
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
    return await handle.query("get_state", result_type=OnboardingState)


async def _unfinished(session_id: str) -> bool:
    try:
        state = await get_state(session_id)
    except RPCError as exc:
        # An onboarding whose workflow is gone has ended long ago.
        if exc.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return state.status not in FINISHED_STATUSES


async def _start(session_id: str, state: OnboardingState) -> None:
    client = await connect_temporal()
    # Started with WaitingForUser, which it keeps, as its delivery does with its findings.
    await client.start_workflow(WORKFLOW_TYPE, state, id=session_id, task_queue=TASK_QUEUE,
                                **execution_fields(state.repository, session_id, flags=True))
    await register(session_id)


async def _discard(session_id: str) -> None:
    """Terminate a fork whose original could not be superseded."""
    try:
        client = await connect_temporal()
        await client.get_workflow_handle(session_id).terminate(
            reason="The original onboarding could not be superseded.")
    except Exception:  # noqa: BLE001 - the original error is what the caller reports
        logger.exception("Could not terminate the fork %s", session_id)


async def start_onboarding(repository: RepositorySelection) -> str:
    """Start onboarding the repository's branch; SessionBusy while another onboarding of
    the same branch is unfinished."""
    full_name, branch = repository["full_name"], repository["branch"]
    session_id = new_session_id()
    async with storage.branch_lock(full_name, branch) as conn:
        for other in await storage.sessions_of(conn, full_name, branch):
            if await _unfinished(other):
                raise SessionBusy(ANOTHER_ONBOARDING)
        await _start(session_id, OnboardingState(repository=repository))
        await storage.add(conn, session_id, full_name, branch)
    logger.info("Started onboarding %s of %s@%s", session_id, full_name, branch)
    return session_id


async def fork_onboarding(session_id: str, message_index: int) -> str:
    """A new onboarding of the same branch with the conversation, drafts and contract
    decision up to the message; the original ends as superseded."""
    source = await get_state(session_id)
    if source.status in FINISHED_STATUSES:
        raise SessionBusy("Only an unfinished onboarding can be forked.")
    if source.status not in ("interviewing", "awaiting_confirmation"):
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
    )
    async with storage.branch_lock(repository["full_name"], repository["branch"]) as conn:
        # The original ends only once the fork that replaces it exists, so that a failed
        # fork never loses the original; only the fork stays unfinished.
        await _start(fork_id, fork)
        try:
            await storage.add(conn, fork_id, repository["full_name"], repository["branch"])
            await start_update(session_id, "supersede")
        except BaseException:
            # The original stays as it was; the fork must not stay unfinished next to it.
            # Leaving the lock's transaction with the error also drops the fork's record.
            await _discard(fork_id)
            raise
    logger.info("Forked onboarding %s after message %d as %s", session_id, message_index,
                fork_id)
    return fork_id


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


async def finalize(session_id: str) -> None:
    await start_update(session_id, "finalize")
    logger.info("Finalizing onboarding %s", session_id)


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
                "contract_problem", "conflicts"):
        del session[key]
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
