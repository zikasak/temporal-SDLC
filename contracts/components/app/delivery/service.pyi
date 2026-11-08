from typing import Any

from temporalio.client import Client

from app.delivery.models import DeliveryProgress
from app.temporal.sessions import SessionBusy


async def connect_temporal() -> Client:
    """The Temporal client this module reaches every delivery through. Besides it, the
    functions below reach Temporal only through app.temporal.sessions (start_update and
    is_running, with that module's connect_temporal), the mention history only through
    app.delivery.mentions (history, recording) and the session registry only through
    app.temporal.registry.register."""
    ...


class MentionHandled(SessionBusy):
    """The delivery already has this mention, such as from an earlier handover."""


async def get_progress(session_id: str) -> DeliveryProgress | None:
    """The progress of the session's delivery (workflow ID delivery_id(session_id)), as
    its `get_state` query reports it, with every mention from the mention history; None
    before the delivery started."""
    ...


async def accept_findings(session_id: str, login: str) -> DeliveryProgress:
    """Accept the open review findings of the session's delivery: the flag
    (open_findings) is cleared, the acceptance recorded (findings_acceptance with
    'accepted_at' and 'accepted_by' `login`) and nothing runs or is pushed. Allowed on a
    delivery that waits and on one whose flow has closed, completed or cancelled because
    its pull request was merged or closed; the progress it returns, and the progress the
    delivery reports afterwards, has the flag cleared. SessionBusy (answered 409) when
    the delivery has no open review findings or is still working."""
    ...


async def instruct_findings(session_id: str, instructions: str) -> DeliveryProgress:
    """Start a fix round from the user's instructions about the open review findings of
    the session's delivery, followed by verification and code review.

    While the delivery is open and waits, as while its pull request is open, the round is
    the delivery's own next run (its `restart` update with a state whose `instructions`
    are these), and its result is pushed to the same branch as the unverified delivery.

    When the delivery's flow has closed (its workflow completed, or was cancelled,
    because its pull request was merged or closed), the closed delivery is not reopened:
    the round runs as a new top-level flow of its own, a new "DeliveryWorkflow" started
    with no parent under a workflow ID other than delivery_id(session_id). Its state
    carries the closed delivery's task under a session ID of its own, so that its result
    is pushed to a new branch and a new pull request is opened from it, and its
    `instructions` are these.

    SessionBusy (answered 409) when the delivery has no open review findings or is still
    working."""
    ...


async def receive_mention(session_id: str, mention: Any) -> bool:
    """Hand an authorised mention of the app on the delivery's pull request (a dict as
    DeliveryWorkflow.mention takes it, with 'repository' and 'pull_number' naming the pull
    request it was made on) to a delivery, through that delivery's `mention` update, and
    wait until the delivery recorded it (app.delivery.mentions.recording of the session
    the delivery belongs to answers '<run ID>:<update ID>' of that update). False when the
    session has no delivery; MentionHandled when it already has the mention.

    While the delivery runs (waits after its push, its pull request open), the mention
    goes to that delivery, and a fix it asks for is pushed to the pull request's branch.

    When the delivery's flow has closed (its workflow completed, or was cancelled,
    because its pull request was merged or closed), the closed delivery is not reopened:
    the mention goes to a new top-level flow of its own, a new "DeliveryWorkflow" started
    with no parent under a workflow ID other than delivery_id(session_id), whose state
    carries the closed delivery's task under a session ID of its own. Its fix round, with
    verification and code review, pushes to a new branch, opens a new pull request from
    it and replies on the old pull request with a link to the new one."""
    ...
