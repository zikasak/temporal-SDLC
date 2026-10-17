"""A session's delivery: its progress, and the manual retry of a failed stage."""

import logging
from datetime import datetime, timezone
from typing import Any

from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.delivery.models import (
    RETRY_STAGES, DeliveryProgress, DeliveryState, RetryStage, delivery_id,
)
from app.requirements.models import CHANGE_KINDS
from app.temporal.client import connect_temporal
from app.temporal.sessions import SessionBusy, start_update

logger = logging.getLogger(__name__)


async def get_progress(session_id: str) -> DeliveryProgress | None:
    """None in the moment between the session finishing and its delivery starting."""
    client = await connect_temporal()
    try:
        state = await client.get_workflow_handle(delivery_id(session_id)).query(
            'get_state', result_type=DeliveryState,
        )
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return None
    return state.progress


async def answer(session_id: str, workflow_id: str, answers: list[str]) -> None:
    """Send the user's answers to the agent of this delivery that waits for them."""
    progress = await get_progress(session_id)
    if not progress or not any(item['workflow_id'] == workflow_id
                               for item in progress.agent_questions):
        raise SessionBusy('These questions are no longer open.')
    await start_update(workflow_id, 'answer_questions', answers)
    logger.info('Sent answers to %s of session %s', workflow_id, session_id)


async def _finished(session_id: str, busy: str) -> tuple[Any, str, DeliveryState]:
    """The client, the task queue and the state of a delivery that is not running."""
    client = await connect_temporal()
    try:
        description = await client.get_workflow_handle(delivery_id(session_id)).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        raise SessionBusy(busy) from exc
    if description.status != WorkflowExecutionStatus.COMPLETED:
        raise SessionBusy(busy)
    # Pin the query to the run we checked, rather than a concurrent restart.
    state = await client.get_workflow_handle(
        delivery_id(session_id), run_id=description.run_id,
    ).query('get_state', result_type=DeliveryState)
    return client, description.task_queue, state


async def _restart(client: Any, task_queue: str, session_id: str,
                   state: DeliveryState) -> None:
    try:
        await client.start_workflow(
            'DeliveryWorkflow', state, id=delivery_id(session_id), task_queue=task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
    except WorkflowAlreadyStartedError as exc:
        raise SessionBusy('Generation is already running for this session.') from exc


async def _flagged(session_id: str) -> tuple[Any, str, DeliveryState]:
    client, task_queue, state = await _finished(
        session_id, 'Wait for the delivery to finish before deciding about its findings.')
    if not state.progress.open_findings:
        raise SessionBusy('The delivery has no open review findings.')
    return client, task_queue, state


async def accept_findings(session_id: str, login: str) -> DeliveryProgress:
    """Keep the delivery pushed with open review findings as it is: the flag is cleared,
    the publication stays unverified and the acceptance is recorded."""
    client, task_queue, state = await _flagged(session_id)
    progress = state.progress
    progress.open_findings = False
    progress.findings_acceptance = {
        'accepted_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'accepted_by': login}
    progress.agent_questions = []
    state.retry_stage = None
    state.instructions = None
    state.record_only = True
    await _restart(client, task_queue, session_id, state)
    logger.info('Open review findings accepted for session %s', session_id)
    return progress


async def instruct_findings(session_id: str, instructions: str) -> DeliveryProgress:
    """Start a fix round from the user's instructions about the open review findings,
    then verification and code review; the result is pushed to the same branch."""
    client, task_queue, state = await _flagged(session_id)
    if not state.spec.language:
        raise SessionBusy('No implementation language was chosen for this session.')
    progress = state.progress
    progress.agent_questions = []
    progress.review_error = None
    progress.verification_status = 'repairing'
    # The flag stays raised and the earlier push stays until a new push replaces it.
    state.retry_stage = None
    state.record_only = False
    state.instructions = instructions
    await _restart(client, task_queue, session_id, state)
    logger.info('Instructions about open review findings sent for session %s', session_id)
    return progress


async def retry(session_id: str, stage: RetryStage) -> DeliveryProgress:
    """Restart a failed stage, retaining the task and the successful build artifacts."""
    if stage not in RETRY_STAGES:
        raise SessionBusy('Choose server generation, tests generation, the documentation, '
                          'the code review or the push to retry.')
    client, task_queue, state = await _finished(
        session_id, 'Wait for the current generation to finish before retrying.')
    progress = state.progress
    state.instructions = None
    state.record_only = False
    if stage == 'review':
        if not progress.review_error or state.spec.kind == 'docs':
            raise SessionBusy('The delivery has no failed code review.')
        # The verified delivery is reviewed again; its verification stays.
        progress.review_error = None
        progress.agent_questions = []
        progress.verification_status = 'reviewing'
        if state.spec.checkout and not progress.open_findings:
            progress.publication_error = None
            progress.publication_status = 'waiting'
        state.retry_stage = stage
        await _restart(client, task_queue, session_id, state)
        logger.info('Restarted the code review for session %s', session_id)
        return progress
    change = state.spec.kind == 'bugfix'
    # A change is published only with a verification that proved all of it.
    proved = state.spec.kind in CHANGE_KINDS
    # A run that could not complete is returned as a result, not recorded as an error.
    unverified = progress.verification_error or (
        progress.verification and progress.verification['status'] == 'error')
    failed = {'server': progress.code_error or progress.implementation_error,
              # The tests of a change are retried with the baseline run that judges them,
              # and that retry is also the way on from a verification that proved nothing.
              'tests': (progress.tests_error or progress.baseline_error
                        or proved and unverified),
              # A documentation task has one stage, which is also the way on from a check
              # of it that could not complete.
              'documentation': (progress.documentation_error
                                or state.spec.kind == 'docs' and unverified),
              'repository': progress.publication_error}[stage]
    if not failed:
        raise SessionBusy('Only failed generation can be retried.')
    if stage == 'tests' and not progress.code and not change:
        raise SessionBusy('Retry server generation first.')
    if not state.spec.language:
        # A completed session takes no messages, so only a fork can choose one.
        raise SessionBusy('No implementation language was chosen for this session. '
                          'Fork it from a message and choose a language there.')
    state.retry_stage = stage
    # A finished run waits for no agent.
    progress.agent_questions = []
    if state.spec.checkout:
        progress.publication_error = None
        progress.publication_status = 'waiting'
    if stage != 'repository':
        # A repeated push keeps the verification it publishes.
        progress.verification = None
        progress.verification_error = None
        progress.existing_tests = None
        progress.verification_status = 'waiting'
        progress.repairs = []
        progress.review_error = None
    if stage == 'server':
        progress.code_error = None
        progress.implementation = None
        progress.implementation_error = None
    elif stage == 'documentation':
        progress.documentation = None
        progress.documentation_error = None
    elif stage == 'tests':
        progress.tests = None
        progress.tests_error = None
        if change:
            progress.baseline = None
            progress.baseline_error = None
            progress.baseline_status = 'waiting'
    await _restart(client, task_queue, session_id, state)
    logger.info('Restarted %s generation for session %s', stage, session_id)
    return progress
