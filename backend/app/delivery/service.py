"""A session's delivery: its progress, and the manual retry of a failed stage."""

import logging

from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.delivery.models import (
    RETRY_STAGES, DeliveryProgress, DeliveryState, RetryStage, delivery_id,
)
from app.temporal.client import connect_temporal
from app.temporal.sessions import SessionBusy

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


async def retry(session_id: str, stage: RetryStage) -> None:
    """Restart a failed stage, retaining the task and the successful build artifacts."""
    if stage not in RETRY_STAGES:
        raise SessionBusy('Choose server generation, tests generation or the push to retry.')
    client = await connect_temporal()
    try:
        description = await client.get_workflow_handle(delivery_id(session_id)).describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        raise SessionBusy('Wait for the current generation to finish before retrying.') from exc
    if description.status != WorkflowExecutionStatus.COMPLETED:
        raise SessionBusy('Wait for the current generation to finish before retrying.')
    # Pin the query to the run we checked, rather than a concurrent restart.
    state = await client.get_workflow_handle(
        delivery_id(session_id), run_id=description.run_id,
    ).query('get_state', result_type=DeliveryState)
    progress = state.progress
    change = state.spec.kind == 'bugfix'
    failed = {'server': progress.code_error or progress.implementation_error,
              # The tests of a change are retried with the baseline run that judges them.
              'tests': (progress.tests_error or progress.baseline_error
                        or change and progress.verification_error),
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
    if state.spec.checkout:
        progress.publication_error = None
        progress.publication_status = 'waiting'
    if stage != 'repository':
        # A repeated push keeps the verification it publishes.
        progress.verification = None
        progress.verification_error = None
        progress.verification_status = 'waiting'
        progress.repairs = []
    if stage == 'server':
        progress.code_error = None
        progress.implementation = None
        progress.implementation_error = None
    elif stage == 'tests':
        progress.tests = None
        progress.tests_error = None
        if change:
            progress.baseline = None
            progress.baseline_error = None
            progress.baseline_status = 'waiting'
    try:
        await client.start_workflow(
            'DeliveryWorkflow', state, id=delivery_id(session_id),
            task_queue=description.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
    except WorkflowAlreadyStartedError as exc:
        raise SessionBusy('Generation is already running for this session.') from exc
    logger.info('Restarted %s generation for session %s', stage, session_id)
