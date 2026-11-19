"""Acceptances of open review findings of deliveries whose flow has closed.

A delivery whose pull request was merged or closed has ended: its workflow can no
longer record the user's acceptance of its open findings, and must not run again for
it. The acceptance is kept in Temporal instead, for the closed run it was given for: as
the memo of a record execution of its own, started on a task queue no worker polls and
terminated at once, so that nothing runs for it. The delivery's progress is reported
with it."""

import logging
from typing import cast

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from app.review.models import FindingsAcceptance

logger = logging.getLogger(__name__)

# The workflow type of the record executions; no worker registers it, and listings of
# flows leave it out.
RECORD_TYPE = 'FindingsAcceptanceRecord'
# A task queue no worker polls, so that a record never runs.
RECORD_QUEUE = 'findings-acceptance-records'
ACCEPTANCE_MEMO = 'findings_acceptance'


async def ensure_schema() -> None:
    """Acceptances are kept in Temporal; there is no schema to create."""


def record_id(session_id: str, run_id: str) -> str:
    return f'findings-acceptance-{session_id}-{run_id}'


async def record(client: Client, session_id: str, run_id: str,
                 acceptance: FindingsAcceptance) -> None:
    """Keep the acceptance of the open findings of the closed run `run_id`. A Temporal
    that cannot be reached raises its error: the acceptance is not reported as given
    when it would be lost."""
    workflow_id = record_id(session_id, run_id)
    handle = await client.start_workflow(
        RECORD_TYPE, id=workflow_id, task_queue=RECORD_QUEUE,
        memo={ACCEPTANCE_MEMO: acceptance},
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.TERMINATE_EXISTING,
    )
    try:
        await handle.terminate(reason='Record of an acceptance of open review findings')
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            logger.warning('Record %s of session %s not terminated: %s',
                           workflow_id, session_id, exc)


async def find(client: Client, session_id: str,
               run_id: str) -> FindingsAcceptance | None:
    """The acceptance of the open findings of the closed run `run_id`, if any."""
    try:
        description = await client.get_workflow_handle(
            record_id(session_id, run_id)).describe()
        acceptance = await description.memo_value(ACCEPTANCE_MEMO, None)
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            logger.warning('Could not read the acceptance of the findings of session %s: %s',
                           session_id, exc)
        return None
    except Exception:  # noqa: BLE001 - an unreadable record counts as none
        logger.warning('Unreadable acceptance of the findings of session %s', session_id,
                       exc_info=True)
        return None
    return cast(FindingsAcceptance, dict(acceptance)) if isinstance(acceptance, dict) else None
