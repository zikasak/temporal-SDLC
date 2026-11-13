"""A session's delivery: its progress, and the manual retry of a failed stage."""

import asyncio
import dataclasses
import hashlib
import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from temporalio.client import Client, WorkflowExecutionDescription, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.config import GENERATED_CODE_DIR
from app.delivery.models import (
    DELIVERY_CLOSING, FOLLOW_UP_PREFIX, MENTION_HANDLED, OUTCOME_OWED, RETRY_STAGES, DeliveryProgress, DeliveryState, RetryStage, delivery_id, interrupted,
    restorable_tests,
)
from app.config import TASK_QUEUE
from app.delivery import acceptances
from app.delivery import mentions as mention_history
from app.delivery.closed_mentions import HANDLED as CLOSED_MENTION_HANDLED
from app.delivery.closed_mentions import WORKFLOW_TYPE as CLOSED_MENTION_TYPE
from app.delivery.closed_mentions import ClosedMention, closed_mention_id
from app.delivery.flow.mention_rounds import CLASSIFIED_AS, fix_unavailable, mention_context
from app.github.checkout import CheckoutError, checkout_path
from app.github.errors import GitHubError
from app.github.models import PullRequestMention
from app.requirements.lookup import lookup_requirements
from app.requirements.models import CHANGE_KINDS
from app.routing.models import SEAM_MISMATCH
from app.task_flow import service as task_flow_service
from app.task_flow.models import fix_task_flow_id, is_task_flow_id
from app.task_flow.stage import STAGE_FLAGS_SIGNAL
from app.temporal import registry
from app.temporal.client import connect_temporal
from app.temporal.execution_fields import task_fields
from app.temporal.sessions import (
    SessionBusy, SessionEnded, SessionNotFound, is_running, start_update,
)

logger = logging.getLogger(__name__)


async def get_progress(session_id: str) -> DeliveryProgress | None:
    """None in the moment between the session finishing and its delivery starting. A
    delivery stopped from outside reports the stage it stopped in as failed, which is
    how a retry finds it. The progress is the one its `get_state` query reports, with
    every mention from the mention history and, for a closed (completed or cancelled)
    delivery, with an acceptance of its open findings recorded after it closed applied:
    `open_findings` is then false and `findings_acceptance` set."""
    state = await get_state(session_id)
    return state.progress if state else None


async def get_state(session_id: str) -> DeliveryState | None:
    """The delivery's state with its progress as get_progress reports it, such as for the
    task as fix rounds changed it; None before the delivery started."""
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
    if state.pending:
        # Created by its task flow, but the stage before it has not handed on yet.
        return None
    if description.status not in (WorkflowExecutionStatus.RUNNING,
                                  WorkflowExecutionStatus.COMPLETED):
        state = interrupted(state)
    state = await _with_acceptance(client, session_id, description, state)
    state.progress = await with_mention_history(session_id, state.progress)
    state.progress.fix_flow_ids = await fix_flow_ids(client, session_id, state)
    return state


async def follow_up_session(session_id: str) -> dict:
    """The flow `session_id` that carries on a closed delivery on a new branch, as a
    requirements session reports it: it has no interview of its own, only the task it
    builds, and its delivery's progress with its fix round, agents' questions, review
    findings and new pull request. SessionNotFound when no such flow started."""
    if not session_id.startswith(FOLLOW_UP_PREFIX):
        raise SessionNotFound('Session not found')
    state = await get_state(session_id)
    if state is None:
        raise SessionNotFound('Session not found')
    spec = state.spec
    brief = spec.brief or {}
    return {
        'session_id': session_id,
        'status': 'completed',
        'kind': spec.kind,
        'messages': [],
        'requirements': list(brief.get('requirements') or []),
        'retired': list(brief.get('retired') or []),
        'lookup_requirements': lookup_requirements(
            brief.get('requirements'), spec.requirements, brief.get('retired')),
        'document': None,
        'error': None,
        'forked_from': None,
        'repository': spec.repository,
        'checkout': spec.checkout,
        'architecture_session_id': None,
        # The task flow the fix round runs in, for a task's delivery.
        'task_flow_id': state.task_flow_id,
        'delivery': dataclasses.asdict(state.progress),
    }


async def _with_acceptance(client: Client, session_id: str,
                           description: WorkflowExecutionDescription,
                           state: DeliveryState) -> DeliveryState:
    """The state of a closed delivery with the acceptance of its open findings that the
    user gave after it closed, which its workflow could not record."""
    if (description.status in CLOSED_STATUSES and state.progress.open_findings
            and description.run_id):
        acceptance = await acceptances.find(client, session_id, description.run_id)
        if acceptance:
            state.progress.open_findings = False
            state.progress.findings_acceptance = acceptance
            state.progress.agent_questions = []
    return state


async def with_mention_history(session_id: str,
                               progress: DeliveryProgress) -> DeliveryProgress:
    """The progress with every mention of the delivery, oldest first, from the mention
    history, as the workflow last reported those it still works on, and with the text of
    the comments that review rounds refer to as their instructions."""
    history = await mention_history.history(session_id)
    key = lambda item: (item['comment_type'], item['comment_id'])  # noqa: E731
    history = await _with_fix_flows(history, key)
    texts = {key(item): item.get('body', '') for item in history}
    current = {key(item): item for item in progress.mentions}
    merged = []
    for item in history:
        live = current.pop(key(item), None)
        merged.append({**item, **live, 'body': item.get('body', '')} if live else item)
    # Mentions the history does not have yet, as the workflow reports them.
    merged.extend({'body': '', **item} for item in current.values())
    # What the workflow still owes a mention is its own bookkeeping, not shown.
    progress.mentions = [{name: value for name, value in item.items() if name != OUTCOME_OWED}
                         for item in merged]
    progress.reviews = [
        _with_instructions(item, texts) for item in progress.reviews]
    return progress


async def _with_fix_flows(history: list[dict], key) -> list[dict]:
    """The mentions of a closed delivery's pull request that a fix round's flow took,
    as that flow reports them: with the flow's ID when they asked for a fix."""
    taken: dict[str, dict] = {}
    result = []
    for item in history:
        follow_up = item.get(FIX_SESSION_KEY)
        if not isinstance(follow_up, str):
            result.append(item)
            continue
        if follow_up not in taken:
            taken[follow_up] = {key(entry): entry
                                for entry in await mention_history.history(follow_up)}
        live = taken[follow_up].get(key(item), {})
        merged = {**item, **{name: value for name, value in live.items()
                             if name in MENTION_OUTCOME_FIELDS}}
        merged.pop(FIX_SESSION_KEY, None)
        if merged.get('classification') != 'fix':
            merged.pop('fix_flow_id', None)
        # Its fix round's review rounds are numbered in the flow that ran it.
        merged.pop('round', None)
        result.append(merged)
    return result


# How a mention handed to the flow of a fix round after its delivery closed is kept with
# that delivery: the session of the flow that took it, and what that flow reports of it.
FIX_SESSION_KEY = 'fix_session_id'
MENTION_OUTCOME_FIELDS = ('classification', 'status', 'reply_url', 'error')


def _with_instructions(round_: dict, texts: dict) -> dict:
    reference = round_.get('mention')
    if not isinstance(reference, dict):
        return round_
    result = {key: value for key, value in round_.items() if key != 'mention'}
    result['instructions'] = texts.get(
        (reference.get('comment_type'), reference.get('comment_id')), '')
    return result


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
    if state.pending:
        raise SessionBusy(busy)
    if description.status == WorkflowExecutionStatus.RUNNING:
        if not state.waiting:
            raise SessionBusy(busy)
    elif description.status != WorkflowExecutionStatus.COMPLETED:
        # Stopped from outside, such as terminated while a stage was stuck.
        state = interrupted(state)
    state = await _with_acceptance(client, session_id, description, state)
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
        # A task's delivery started again on its own is still listed under its task flow.
        await finished.client.start_workflow(
            'DeliveryWorkflow', finished.state, id=workflow_id,
            task_queue=description.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            **task_fields(finished.state.spec, finished.state.task_flow_id),
        )
    except WorkflowAlreadyStartedError as exc:
        raise SessionBusy(busy) from exc


CLOSED_STATUSES = (WorkflowExecutionStatus.COMPLETED, WorkflowExecutionStatus.CANCELED)
# How many follow-up flows of one closed delivery are looked through for the open one.
MAX_FOLLOW_UPS = 50
FOLLOW_UP_BUSY = 'The fix round of this closed flow is still running.'


def _closed(finished: _Finished) -> bool:
    """Whether the delivery's flow has closed: its workflow completed, or was cancelled,
    such as because its pull request was merged or closed."""
    return finished.description.status in CLOSED_STATUSES


# How long reading a pull request's state from GitHub may take before it counts as unknown.
PULL_REQUEST_STATE_SECONDS = 15


async def _pull_request_state(full_name: object, number: object) -> str | None:
    """How GitHub reports the pull request now: 'open', 'merged' or 'closed' (without
    merging); None when it cannot be read, such as without a connection, when GitHub
    cannot be reached or answers 404, which says nothing about the pull request."""
    if not isinstance(full_name, str) or type(number) is not int or number <= 0:
        return None
    from app.github import client as github_client
    from app.github import service as github_service
    from app.github import storage as github_storage

    async def read() -> dict | None:
        connection = await github_storage.get_connection()
        token = await github_service.installation_token(connection)
        return await github_client.get_pull_request_state(token, full_name, number)
    try:
        current = await asyncio.wait_for(read(), PULL_REQUEST_STATE_SECONDS)
    except Exception as exc:  # noqa: BLE001 - an unknown state falls back to the delivery's
        logger.warning('State of pull request #%s of %s not read: %s', number, full_name, exc)
        return None
    if current is None:
        return None
    if current.get('merged'):
        return 'merged'
    return 'closed' if current.get('state') == 'closed' else 'open'


def _pull_request_of(state: DeliveryState) -> tuple[str | None, int | None]:
    """The repository and number of the delivery's pull request, if it has one."""
    pull = state.progress.pull_request or {}
    repository = state.spec.repository or {}
    return repository.get('full_name'), pull.get('number')


async def _report_closed(client: Client, session_id: str, number: int,
                         merged: bool) -> None:
    """Tell the running delivery its pull request was merged or closed, as the webhook
    would, and wait a little for it to close."""
    workflow_id = delivery_id(session_id)
    try:
        await client.get_workflow_handle(workflow_id).signal(
            'pull_request_closed', args=[number, merged])
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return
    logger.info('Told the delivery of session %s that pull request #%d was %s',
                session_id, number, 'merged' if merged else 'closed')
    await _closed_soon(client, workflow_id)


async def _as_pull_request_stands(finished: _Finished, session_id: str,
                                  busy: str) -> tuple[_Finished, bool]:
    """The delivery as its pull request's actual state leaves it, and whether its flow
    has closed with the pull request. A running delivery whose pull request was merged or
    closed, which neither the webhook nor its check reported yet, is told and closes; a
    delivery that closed while its pull request is still open, such as one idle for long,
    has not closed with it. When the state cannot be read, the delivery's status says."""
    full_name, number = _pull_request_of(finished.state)
    current = await _pull_request_state(full_name, number)
    status = finished.description.status
    if status == WorkflowExecutionStatus.RUNNING and current in ('merged', 'closed'):
        await _report_closed(finished.client, session_id, number, current == 'merged')
        finished = await _finished(session_id, busy)
        status = finished.description.status
    if status not in CLOSED_STATUSES:
        return finished, False
    return finished, current != 'open'


def follow_up_session_id(session_id: str, number: int) -> str:
    """The session of the `number`th flow that carries on the closed delivery of
    `session_id` (from 1): derived from it, so that a handover tried again finds the flow
    it started, and named like a session so that it pushes to a branch of its own."""
    digest = hashlib.sha256(f'{session_id}:follow-up:{number}'.encode()).hexdigest()[:32]
    return f'{FOLLOW_UP_PREFIX}{digest}'


def _follow_up_state(state: DeliveryState, session_id: str) -> DeliveryState:
    """The closed delivery's task as a flow of its own: the same task, its built
    artifacts and its last push to start from, under a session of its own, whose result
    goes to a new branch and a new pull request. It records nothing until a fix round
    starts, and has none of the closed delivery's mentions or pull request."""
    spec = dataclasses.replace(state.spec, session_id=session_id)
    progress = dataclasses.replace(
        state.progress, mentions=[], agent_questions=[], pull_request=None,
        pull_request_error=None, pull_request_status='skipped')
    return dataclasses.replace(
        state, spec=spec, progress=progress, retry_stage=None, instructions=None,
        record_only=True, mention=None, mention_type=None, mention_queue=[],
        waiting=False, task_flow_id=None, pending=False)


async def _copy_workspace(source: str, target: str) -> None:
    """Give the follow-up session the closed one's working copy and build artifacts, which
    its fix round changes and pushes; without them its stages report what is missing."""
    def copy() -> None:
        try:
            origin = checkout_path(GENERATED_CODE_DIR, source).parent
            destination = checkout_path(GENERATED_CODE_DIR, target).parent
        except CheckoutError:
            return
        if not origin.is_dir() or destination.exists():
            return
        shutil.copytree(origin, destination, symlinks=True)
    try:
        await asyncio.to_thread(copy)
    except OSError as exc:
        logger.warning('Working copy of session %s not copied to %s: %s', source, target, exc)


def _carries_task(state: DeliveryState) -> bool:
    """Whether the delivery is a task's, whose fix rounds after it closed run in task
    flows of their own; an onboarding's run as top-level deliveries."""
    return not state.spec.onboarding


def fix_flow_id(state: DeliveryState, follow_up: str) -> str:
    """The new top-level flow of the fix round run as `follow_up`: a task flow for a
    task's delivery, the fix round's own delivery for an onboarding."""
    if _carries_task(state):
        return fix_task_flow_id(follow_up)
    return delivery_id(follow_up)


async def fix_flow_ids(client: Client, session_id: str, state: DeliveryState) -> list[str]:
    """The flows the fix rounds started after the delivery of `session_id` closed, oldest
    first; a flow carrying on a closed delivery numbers its own."""
    found = []
    for number in range(1, MAX_FOLLOW_UPS + 1):
        follow_up = follow_up_session_id(session_id, number)
        try:
            await client.get_workflow_handle(delivery_id(follow_up)).describe()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            break
        found.append(fix_flow_id(state, follow_up))
    return found


async def _start_follow_up(finished: _Finished, session_id: str, state: DeliveryState,
                           workflow_id: str) -> None:
    """Start the fix round's flow: the delivery child of a new task flow for a task's
    delivery, linked to the task's stages; a top-level delivery for an onboarding."""
    if _carries_task(state):
        stages = [item for item in (finished.description.parent_id,
                                    delivery_id(session_id)) if item]
        await task_flow_service.start_fix(
            fix_task_flow_id(state.spec.session_id), state, finished.state.task_flow_id,
            stages)
        return
    await finished.client.start_workflow(
        'DeliveryWorkflow', state, id=workflow_id,
        task_queue=finished.description.task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        **task_fields(state.spec),
    )


async def _follow_up(finished: _Finished, session_id: str,
                     prepare=None) -> tuple[str, bool]:
    """The open flow that carries on the closed delivery of `session_id`, as its session
    ID, started when there is none: the earliest of its follow-ups whose workflow does
    not exist yet or still runs. For a task's delivery it runs as the delivery of a new
    task flow, for an onboarding's as a new top-level DeliveryWorkflow. `prepare(state)`
    changes the state a new one starts from. Also true when this call started it."""
    client = finished.client
    for number in range(1, MAX_FOLLOW_UPS + 1):
        follow_up = follow_up_session_id(session_id, number)
        workflow_id = delivery_id(follow_up)
        try:
            description = await client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
        else:
            if description.status == WorkflowExecutionStatus.RUNNING:
                return follow_up, False
            continue
        state = _follow_up_state(finished.state, follow_up)
        if prepare is not None:
            prepare(state)
        await _copy_workspace(session_id, follow_up)
        try:
            await _start_follow_up(finished, session_id, state, workflow_id)
        except WorkflowAlreadyStartedError:
            # Started concurrently, such as by another handover: that one is the flow.
            return follow_up, False
        await registry.register(workflow_id)
        logger.info('Delivery of session %s closed; carried on as the flow %s',
                    session_id, workflow_id)
        return follow_up, True
    raise SessionBusy('This closed flow has been carried on too often.')


async def _flagged(session_id: str) -> _Finished:
    finished = await _finished(
        session_id, 'Wait for the delivery to finish before deciding about its findings.')
    if not finished.state.progress.open_findings:
        raise SessionBusy('The delivery has no open review findings.')
    return finished


async def _clear_task_flow_marker(finished: _Finished, session_id: str) -> None:
    """Tell the task flow of the closed delivery, which may still be open, such as while
    linked stages of another task run, that its findings were accepted: the closed
    delivery can no longer report it, and the flow's marker disappears with it."""
    task_flow_id = finished.state.task_flow_id or finished.description.parent_id
    if not task_flow_id or not is_task_flow_id(task_flow_id):
        return
    try:
        await finished.client.get_workflow_handle(task_flow_id).signal(
            STAGE_FLAGS_SIGNAL, args=[delivery_id(session_id), False, False])
    except RPCError as exc:
        # A closed task flow is listed without the marker from the acceptance record.
        if exc.status != RPCStatusCode.NOT_FOUND:
            logger.warning('Task flow %s not told that the findings of session %s were '
                           'accepted: %s', task_flow_id, session_id, exc)


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
    state.mention = None
    state.mention_type = None
    state.record_only = True
    if _closed(finished):
        # The flow closed with its pull request: it is not reopened, and nothing runs.
        # The acceptance is kept for the closed run and reported with its progress.
        await acceptances.record(finished.client, session_id, finished.description.run_id,
                                 progress.findings_acceptance)
        await _clear_task_flow_marker(finished, session_id)
        logger.info('Open review findings accepted for the closed session %s', session_id)
        return progress
    await _restart(finished, session_id)
    logger.info('Open review findings accepted for session %s', session_id)
    return progress


async def instruct_findings(session_id: str, instructions: str) -> DeliveryProgress:
    """Start a fix round from the user's instructions about the open review findings,
    then verification and code review. While the pull request is open, the result is
    pushed to the same branch; once it was merged or closed, the round runs as a flow of
    its own on a new branch."""
    finished = await _flagged(session_id)
    finished, closed = await _as_pull_request_stands(
        finished, session_id,
        'Wait for the delivery to finish before deciding about its findings.')
    if not finished.state.progress.open_findings:
        raise SessionBusy('The delivery has no open review findings.')
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
    progress.component_tests_error = None
    progress.verification_status = 'repairing'
    # The flag stays raised and the earlier push stays until a new push replaces it.
    state.retry_stage = None
    state.record_only = False
    state.instructions = instructions
    state.mention = None
    state.mention_type = None
    if closed:
        # The flow closed with its pull request: the round runs as a flow of its own,
        # pushed to a new branch with a new pull request, and the closed one stays.
        def instructed(follow_up: DeliveryState) -> None:
            follow_up.record_only = False
            follow_up.instructions = instructions
        follow_up, started = await _follow_up(finished, session_id, instructed)
        if not started:
            # The flow that carries on the closed one takes the round when it waits.
            current = await finished.client.get_workflow_handle(
                delivery_id(follow_up)).query('get_state', result_type=DeliveryState)
            if not current.waiting:
                raise SessionBusy(FOLLOW_UP_BUSY)
            current.retry_stage = None
            current.record_only = False
            current.instructions = instructions
            current.mention = None
            current.mention_type = None
            current.progress.agent_questions = []
            current.progress.review_error = None
            current.progress.correction_failed = False
            current.progress.verification_status = 'repairing'
            try:
                await start_update(delivery_id(follow_up), 'restart', current)
            except SessionBusy as exc:
                raise SessionBusy(FOLLOW_UP_BUSY) from exc
        logger.info('Instructions about open review findings of the closed session %s '
                    'started the flow of %s', session_id, follow_up)
        return progress
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
    state.mention = None
    state.mention_type = None
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
              # So is a routing that failed or found a criterion no suite can observe, and
              # a seam that could not be added to the code.
              'tests': (progress.tests_error or progress.component_tests_error
                        or progress.baseline_error or progress.routing_error
                        or progress.seam_error or proved and unverified),
              # A documentation task has one stage, which is also the way on from a check
              # of it that could not complete.
              'documentation': (progress.documentation_error
                                or state.spec.kind == 'docs' and unverified),
              # A pull request step that ended in error is retried with a new push, and so
              # is one that fell back to the generic title and description.
              'repository': (progress.publication_error or progress.pull_request_error
                             or progress.pull_request_text_error)}[stage]
    if not failed:
        raise SessionBusy('Only failed generation can be retried.')
    # An onboarding generates no code: its tests are written for the existing code.
    if stage == 'tests' and not progress.code and not change and not state.spec.onboarding:
        raise SessionBusy('Retry server generation first.')
    if state.spec.architecture_only and stage != 'repository':
        raise SessionBusy('An onboarding that adds only the architecture has only its push '
                          'to retry.')
    if not state.spec.language and not state.spec.architecture_only:
        # A completed session takes no messages, so only a fork can choose one.
        raise SessionBusy('No implementation language was chosen for this session. '
                          'Fork it from a message and choose a language there.')
    state.retry_stage = stage
    # A suite rejected for changing tests the changed service still passes would be
    # rejected again: the retry has its author put them back, before the run is cleared.
    state.restore_tests = (restorable_tests(progress)
                           if stage == 'tests' and state.spec.kind == 'api_change' else [])
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
        progress.component_verification = None
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
        progress.component_tests = None
        progress.component_tests_error = None
        if change:
            # A seam the change added is added again when the criteria are routed again,
            # or when the baseline against the code with it proved nothing: it may itself
            # have changed what the tests observe. A seam that was not added is retried.
            # A seam that does not match its contract is retried from a new routing: the
            # same contract would fail the same way when that is what is wrong.
            mismatch = (progress.seam_status == 'error'
                        and (progress.seam_error or '').startswith(SEAM_MISMATCH))
            if (progress.seam_status == 'error' or progress.routing_status != 'routed'
                    or progress.baseline_status in ('not_reproduced', 'error')):
                progress.seam = None
                progress.seam_error = None
                progress.seam_status = 'not_applicable'
                progress.seam_reviews = []
            progress.test_reviews = []
            progress.baseline = None
            progress.baseline_error = None
            progress.component_baseline = None
            progress.baseline_status = 'waiting'
            # Criteria were routed again unless every one had a suite that observes it.
            progress.routing_error = None
            if progress.routing_status == 'unobservable':
                # Criteria a test author declined are routed again told why.
                state.declined_routes = {
                    requirement: route for requirement, route in (progress.routing or {}).items()
                    if route['source'] == 'tests'} or None
            if progress.routing_status != 'routed' or mismatch:
                progress.routing = None
                progress.routing_status = 'waiting'
    await _restart(finished, session_id)
    logger.info('Restarted %s generation for session %s', stage, session_id)
    return progress


# How long a mention waits for a closed delivery to run again before it is handed over.
MENTION_RESTART_POLLS = 40
MENTION_RESTART_POLL_SECONDS = 0.25
# How often a mention tries again when the delivery closed or was reopened concurrently.
MENTION_HANDOVER_ATTEMPTS = 3
# How long a handover waits for the delivery to record the mention it accepted.
MENTION_ADMISSION_POLLS = 120
MENTION_ADMISSION_POLL_SECONDS = 0.25


class MentionHandled(SessionBusy):
    """The delivery already has this mention, such as from an earlier handover."""


async def _reopen(client: Client, description: WorkflowExecutionDescription,
                  session_id: str) -> None:
    """Start the closed delivery's next run, changing nothing, and wait until it runs. A
    concurrent handover or decision of the user's that reopened it first is as good."""
    workflow_id = delivery_id(session_id)
    state = await client.get_workflow_handle(
        workflow_id, run_id=description.run_id,
    ).query('get_state', result_type=DeliveryState)
    if description.status != WorkflowExecutionStatus.COMPLETED:
        state = interrupted(state)
    # An acceptance of its open findings given after it closed goes on with the next run.
    state = await _with_acceptance(client, session_id, description, state)
    state.waiting = False
    state.retry_stage = None
    state.instructions = None
    state.mention = None
    state.mention_type = None
    state.record_only = True
    state.progress.agent_questions = []
    try:
        await _restart(_Finished(client, description, state), session_id)
    except SessionBusy:
        # Another request restarted it in the meantime; its run takes the mention.
        logger.info('Delivery of session %s was reopened concurrently', session_id)
    for _ in range(MENTION_RESTART_POLLS):
        if await is_running(workflow_id):
            return
        await asyncio.sleep(MENTION_RESTART_POLL_SECONDS)


async def _admitted(session_id: str, mention: PullRequestMention, update_id: str) -> None:
    """Wait until the delivery durably recorded the mention handed over by the update
    `update_id`, without waiting for its classification or reply. MentionHandled when it
    was recorded by another handover, which handled the comment before; a GitHubError
    (503) when the recording is not known in time, leaving the claim to be handed over
    again, which the delivery then admits or rejects."""
    for _ in range(MENTION_ADMISSION_POLLS):
        recorded_by = await mention_history.recording(
            session_id, mention['comment_type'], mention['comment_id'])
        if recorded_by is not None:
            # The workflow records as '<run ID>:<update ID>'.
            if recorded_by.rpartition(':')[2] == update_id:
                return
            raise MentionHandled(MENTION_HANDLED)
        await asyncio.sleep(MENTION_ADMISSION_POLL_SECONDS)
    raise GitHubError(503, 'The delivery has not recorded the mention yet.')


async def _running_follow_up(client: Client, session_id: str) -> str | None:
    """The session of the flow of a fix round that carries on the closed delivery of
    `session_id` and still runs, if any; none is started."""
    for number in range(1, MAX_FOLLOW_UPS + 1):
        follow_up = follow_up_session_id(session_id, number)
        try:
            description = await client.get_workflow_handle(delivery_id(follow_up)).describe()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            return None
        if description.status == WorkflowExecutionStatus.RUNNING:
            return follow_up
    return None


# How long a handover waits for a mention on a closed delivery's pull request to be
# classified; one still unclassified then stays claimed and is handed over again later.
CLASSIFY_POLLS = 240
CLASSIFY_POLL_SECONDS = 0.25


async def _classify_closed(client: Client, session_id: str, state: DeliveryState,
                           mention: PullRequestMention) -> str:
    """Record and classify the mention on the pull request of the closed delivery of
    `session_id`, in a workflow of its own that answers it right away unless it is a
    fix request. Its classification: 'fix' when a fix round's flow is to take it;
    MentionHandled when the delivery already had it. A handover tried again finds the
    workflow it started and its classification."""
    workflow_id = closed_mention_id(session_id, mention['comment_type'],
                                    mention['comment_id'])
    start = ClosedMention(
        session_id=session_id, mention=dict(mention),
        context=mention_context(state.spec, state.progress),
        fix_problem=fix_unavailable(state.spec, state.progress))
    try:
        handle = await client.start_workflow(
            CLOSED_MENTION_TYPE, start, id=workflow_id, task_queue=TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
    except WorkflowAlreadyStartedError:
        # Started by an earlier handover of the same comment, which may have ended.
        handle = client.get_workflow_handle(workflow_id)
    for _ in range(CLASSIFY_POLLS):
        description = await handle.describe()
        if description.status == WorkflowExecutionStatus.COMPLETED:
            classification = await handle.result()
        elif description.status != WorkflowExecutionStatus.RUNNING:
            raise GitHubError(503, 'The mention could not be classified.')
        else:
            classification = await handle.query('classification')
        if classification == CLOSED_MENTION_HANDLED:
            raise MentionHandled(MENTION_HANDLED)
        if classification is not None:
            logger.info('Mention %s on the closed pull request of session %s: %s',
                        mention['comment_id'], session_id, classification)
            return classification
        await asyncio.sleep(CLASSIFY_POLL_SECONDS)
    raise GitHubError(503, 'The mention has not been classified yet.')


async def receive_mention(session_id: str, mention: PullRequestMention) -> bool:
    """Hand an authorised mention of the app on the delivery's pull request to the
    delivery, which classifies it and replies or queues its fix round. A delivery that
    closed, such as one idle for long, runs again first, changing nothing; concurrent
    mentions that find it closed each hand theirs to the run that one of them started.
    False when the session has no delivery; MentionHandled when it already has the
    mention."""
    client = await connect_temporal()
    workflow_id = delivery_id(session_id)
    # Which branch takes the fix is decided by the pull request's actual state: its own
    # while it is open, a new one once it was merged or closed. Unknown, such as when
    # GitHub cannot be read, the delivery's status decides.
    pull_number = mention.get('pull_number') if isinstance(mention, dict) else None
    pull_repository = mention.get('repository') if isinstance(mention, dict) else None
    current = await _pull_request_state(pull_repository, pull_number)
    # Set once the delivery rejected the mention because its pull request was merged or
    # closed: a reading of 'open' taken before then no longer holds.
    ended = False
    # Set once the mention was classified as a fix request for the closed delivery.
    known_fix = False
    for attempt in range(1, MENTION_HANDOVER_ATTEMPTS + 1):
        try:
            description = await client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            return False
        if (description.status == WorkflowExecutionStatus.RUNNING
                and current in ('merged', 'closed')):
            # Its pull request ended, which the delivery has not been told yet: it closes
            # first, so that the fix goes to a new branch rather than the ended one's.
            await _report_closed(client, session_id, pull_number, current == 'merged')
            try:
                description = await client.get_workflow_handle(workflow_id).describe()
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise
                return False
        # The session whose delivery takes the mention: this one's while it runs or its
        # pull request is open, or the flow that carries on a delivery whose flow closed
        # with its pull request.
        target = session_id
        handed = mention
        if description.status in CLOSED_STATUSES and (ended or current != 'open'):
            state = await client.get_workflow_handle(
                workflow_id, run_id=description.run_id,
            ).query('get_state', result_type=DeliveryState)
            state = await _with_acceptance(client, session_id, description, state)
            running = await _running_follow_up(client, session_id)
            if running is not None:
                # The flow of an earlier fix round still runs: it takes the comment.
                target = running
            else:
                # Only a fix request starts a flow; anything else is answered for the
                # closed delivery, without one.
                if await _classify_closed(client, session_id, state, mention) != 'fix':
                    return True
                known_fix = True

                def mentioned(follow_up: DeliveryState) -> None:
                    # Its fix round is the comment's, not the closed delivery's findings.
                    follow_up.progress.open_findings = False
                target, _ = await _follow_up(_Finished(client, description, state),
                                             session_id, mentioned)
            if known_fix:
                handed = {**mention, CLASSIFIED_AS: 'fix'}
        elif description.status != WorkflowExecutionStatus.RUNNING:
            await _reopen(client, description, session_id)
        update_id = f'mention-{uuid.uuid4().hex}'
        try:
            await start_update(delivery_id(target), 'mention', handed, update_id=update_id)
        except SessionEnded:
            # It closed before the update reached it, perhaps with its pull request: the
            # pull request's state is read again, and the mention handed over again.
            if attempt == MENTION_HANDOVER_ATTEMPTS:
                raise
            current = await _pull_request_state(pull_repository, pull_number)
            continue
        except SessionBusy as exc:
            if MENTION_HANDLED in str(exc):
                raise MentionHandled(str(exc)) from exc
            if DELIVERY_CLOSING in str(exc) and attempt < MENTION_HANDOVER_ATTEMPTS:
                # Its pull request was just merged or closed: once the delivery has
                # closed, the flow that carries it on takes the mention, never a reopened
                # run of the closed delivery on the ended pull request's branch.
                ended = True
                await _closed_soon(client, delivery_id(target))
                current = await _pull_request_state(pull_repository, pull_number)
                continue
            raise
        # Accepted is not yet admitted: a comment the delivery handled before, which has
        # left its live state, is only found by its durable recording.
        await _admitted(target, mention, update_id)
        if target != session_id:
            # Kept with the closed delivery too, pointing at the flow of its fix round.
            await mention_history.save(session_id, {
                **mention,
                **({'classification': 'fix', 'status': 'queued'} if known_fix
                   else {'classification': 'pending', 'status': 'classifying'}),
                FIX_SESSION_KEY: target, 'fix_flow_id': fix_flow_id(state, target)})
        logger.info('Mention %s on the pull request of session %s handed to the delivery '
                    'of %s', mention['comment_id'], session_id, target)
        return True
    return False


async def pull_request_closed(session_id: str, number: int, merged: bool) -> bool:
    """Tell the session's delivery that its pull request `number` was closed, merged when
    `merged` is true, so that it and its flow close; false when it has no delivery that
    runs."""
    client = await connect_temporal()
    try:
        await client.get_workflow_handle(delivery_id(session_id)).signal(
            'pull_request_closed', args=[number, merged])
    except RPCError as exc:
        # A closed or missing delivery has nothing left to close.
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return False
    return True


async def check_waiting_pull_requests() -> int:
    """Ask every running delivery to check its pull request now, such as when the workers
    start: a delivery that began to wait before deliveries checked their pull request
    moves on to a run that does. The number asked."""
    client = await connect_temporal()
    asked = 0
    async for item in client.list_workflows(
            "WorkflowType = 'DeliveryWorkflow' AND ExecutionStatus = 'Running'"):
        try:
            await client.get_workflow_handle(item.id).signal('check_pull_request')
        except RPCError as exc:
            logger.warning('Delivery %s not asked to check its pull request: %s', item.id, exc)
            continue
        asked += 1
    return asked


async def _closed_soon(client: Client, workflow_id: str) -> None:
    """Wait a little for a delivery that is closing to have closed."""
    for _ in range(MENTION_RESTART_POLLS):
        try:
            description = await client.get_workflow_handle(workflow_id).describe()
        except RPCError:
            return
        if description.status != WorkflowExecutionStatus.RUNNING:
            return
        await asyncio.sleep(MENTION_RESTART_POLL_SECONDS)
