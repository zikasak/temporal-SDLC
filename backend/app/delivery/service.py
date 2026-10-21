"""A session's delivery: its progress, and the manual retry of a failed stage."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from temporalio.client import Client, WorkflowExecutionDescription, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.delivery.models import (
    RETRY_STAGES, DeliveryProgress, DeliveryState, RetryStage, delivery_id, interrupted,
)
from app.requirements.models import CHANGE_KINDS
from app.temporal.client import connect_temporal
from app.temporal.sessions import SessionBusy, is_running, start_update

logger = logging.getLogger(__name__)


async def get_progress(session_id: str) -> DeliveryProgress | None:
    """None in the moment between the session finishing and its delivery starting. A
    delivery stopped from outside reports the stage it stopped in as failed, which is
    how a retry finds it."""
    client = await connect_temporal()
    handle = client.get_workflow_handle(delivery_id(session_id))
    try:
        description = await handle.describe()
        state = await client.get_workflow_handle(
            delivery_id(session_id), run_id=description.run_id,
        ).query('get_state', result_type=DeliveryState)
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return None
    if description.status not in (WorkflowExecutionStatus.RUNNING,
                                  WorkflowExecutionStatus.COMPLETED):
        state = interrupted(state)
    return state.progress


async def answer(session_id: str, workflow_id: str, answers: list[str]) -> None:
    """Send the user's answers to the agent of this delivery that waits for them."""
    progress = await get_progress(session_id)
    if not progress or not any(item['workflow_id'] == workflow_id
                               for item in progress.agent_questions):
        raise SessionBusy('These questions are no longer open.')
    await start_update(workflow_id, 'answer_questions', answers)
    logger.info('Sent answers to %s of session %s', workflow_id, session_id)


@dataclass
class _Finished:
    """A delivery that is not working: its latest run waits for the user's decision, or
    closed. `state` is what the next run starts from."""

    client: Client
    description: WorkflowExecutionDescription
    state: DeliveryState


async def _finished(session_id: str, busy: str) -> _Finished:
    client = await connect_temporal()
    try:
        description = await client.get_workflow_handle(delivery_id(session_id)).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        raise SessionBusy(busy) from exc
    # Pin the query to the run we checked, rather than a concurrent restart.
    state = await client.get_workflow_handle(
        delivery_id(session_id), run_id=description.run_id,
    ).query('get_state', result_type=DeliveryState)
    if description.status == WorkflowExecutionStatus.RUNNING:
        if not state.waiting:
            raise SessionBusy(busy)
    elif description.status != WorkflowExecutionStatus.COMPLETED:
        # Stopped from outside, such as terminated while a stage was stuck.
        state = interrupted(state)
    return _Finished(client, description, state)


async def _restart(finished: _Finished, session_id: str) -> None:
    """Start the next run of the delivery where the task's workflow tree keeps it: a
    waiting delivery continues as new, and a stopped one is started again by its session.
    A delivery whose session closed, such as one from before sessions waited for their
    deliveries, can only run on its own."""
    workflow_id = delivery_id(session_id)
    busy = 'Generation is already running for this session.'
    description = finished.description
    if description.status == WorkflowExecutionStatus.RUNNING:
        try:
            await start_update(workflow_id, 'restart', finished.state)
        except SessionBusy as exc:
            raise SessionBusy(busy) from exc
        return
    parent = description.parent_id
    if parent and await is_running(parent):
        try:
            await start_update(parent, 'restart_delivery', finished.state)
        except SessionBusy as exc:
            raise SessionBusy(busy) from exc
        return
    try:
        await finished.client.start_workflow(
            'DeliveryWorkflow', finished.state, id=workflow_id,
            task_queue=description.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
    except WorkflowAlreadyStartedError as exc:
        raise SessionBusy(busy) from exc


async def _flagged(session_id: str) -> _Finished:
    finished = await _finished(
        session_id, 'Wait for the delivery to finish before deciding about its findings.')
    if not finished.state.progress.open_findings:
        raise SessionBusy('The delivery has no open review findings.')
    return finished


async def accept_findings(session_id: str, login: str) -> DeliveryProgress:
    """Keep the delivery pushed with open review findings as it is: the flag is cleared,
    the publication stays unverified and the acceptance is recorded."""
    finished = await _flagged(session_id)
    state = finished.state
    progress = state.progress
    progress.open_findings = False
    progress.findings_acceptance = {
        'accepted_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'accepted_by': login}
    progress.agent_questions = []
    state.retry_stage = None
    state.instructions = None
    state.record_only = True
    await _restart(finished, session_id)
    logger.info('Open review findings accepted for session %s', session_id)
    return progress


async def instruct_findings(session_id: str, instructions: str) -> DeliveryProgress:
    """Start a fix round from the user's instructions about the open review findings,
    then verification and code review; the result is pushed to the same branch."""
    finished = await _flagged(session_id)
    state = finished.state
    if not state.spec.language:
        raise SessionBusy('No implementation language was chosen for this session.')
    progress = state.progress
    progress.agent_questions = []
    progress.review_error = None
    progress.correction_failed = False
    # A failed earlier fix round keeps its images; its errors would stop this round.
    progress.implementation_error = None
    progress.tests_error = None
    progress.verification_status = 'repairing'
    # The flag stays raised and the earlier push stays until a new push replaces it.
    state.retry_stage = None
    state.record_only = False
    state.instructions = instructions
    await _restart(finished, session_id)
    logger.info('Instructions about open review findings sent for session %s', session_id)
    return progress


async def retry(session_id: str, stage: RetryStage) -> DeliveryProgress:
    """Restart a failed stage, retaining the task and the successful build artifacts."""
    if stage not in RETRY_STAGES:
        raise SessionBusy('Choose server generation, tests generation, the documentation, '
                          'the code review or the push to retry.')
    finished = await _finished(
        session_id, 'Wait for the current generation to finish before retrying.')
    state = finished.state
    progress = state.progress
    state.instructions = None
    state.record_only = False
    if stage == 'review':
        if not (progress.review_error or progress.correction_failed) or state.spec.kind == 'docs':
            raise SessionBusy('The delivery has no failed code review or correction.')
        # The verified delivery is reviewed again, its verification staying, or the failed
        # correction of review findings goes on.
        progress.agent_questions = []
        progress.verification_status = 'repairing' if progress.correction_failed else 'reviewing'
        # The run says by the status that it goes on from the correction; the page would
        # otherwise show the failure again until the run gets there.
        progress.correction_failed = False
        progress.review_error = None
        if state.spec.checkout and not progress.open_findings:
            progress.publication_error = None
            progress.publication_status = 'waiting'
            progress.pull_request_error = None
            progress.pull_request_status = 'waiting'
        state.retry_stage = stage
        await _restart(finished, session_id)
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
              # A pull request step that ended in error is retried with a new push.
              'repository': progress.publication_error or progress.pull_request_error}[stage]
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
        progress.pull_request_error = None
        progress.pull_request_status = 'waiting'
    if stage != 'repository':
        # A repeated push keeps the verification it publishes.
        progress.verification = None
        progress.verification_error = None
        progress.existing_tests = None
        progress.verification_status = 'waiting'
        progress.repairs = []
        progress.review_error = None
        progress.correction_failed = False
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
    await _restart(finished, session_id)
    logger.info('Restarted %s generation for session %s', stage, session_id)
    return progress
