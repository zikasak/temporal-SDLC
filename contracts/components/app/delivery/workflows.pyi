from typing import Any

from app.delivery.models import DeliveryProgress, DeliveryState


class DeliveryWorkflow:
    """Turns a specified task into verified, published sources and pushes them to a
    branch of the repository, opening a pull request from it. Registered under the
    workflow name "DeliveryWorkflow"; its workflow ID is delivery_id(session ID). Every
    activity it runs, by its registered name, runs on the delivery's own task queue, so a
    test registers fakes of them on the worker that runs the delivery.

    A finished run waits for the user's next decision (the `restart` update) or a
    mention on its pull request, until its pull request is merged or closed. A run that
    pushed but could not open a pull request has nothing to wait for and closes right
    after the push."""

    def __init__(self, state: DeliveryState) -> None:
        """The delivery of `state.spec`, starting from `state`."""
        ...

    async def run(self, state: DeliveryState) -> DeliveryProgress:
        """Carry out the task, or with `state.record_only` only record `state`, then wait.

        The push of an architecture-only onboarding (`spec.architecture_only` and
        `spec.onboarding`, with `spec.checkout` and `spec.repository`), the simplest run
        that pushes, runs these activities in turn:
        - 'publish_onboarding_architecture' with args [session_id, repository,
          architecture, architecture_structure], returning the publication: {'branch',
          'commit', 'url', 'verified', 'ignored', 'ignored_count'};
        - 'write_pull_request_text' with args [session_id, publication, change],
          returning {'title', 'summary'}; its failure only falls back to a generic text;
        - 'open_pull_request' with args [session_id, repository, publication, change,
          text], returning the pull request {'number', 'title', 'url', 'draft', 'base'}.
          It fails with an ApplicationError of type 'PullRequestError', non-retryable,
          when no pull request can be opened, such as when the pull_requests permission
          is missing; the push then still counts (publication_status 'completed') and
          pull_request_status is 'error'.

        After its work the run waits (state.waiting is raised):
        - When the run pushed but has no pull request (no pull request could be opened),
          it does not wait: it completes right after the push, and the workflow closes
          as completed, returning its progress.
        - While it waits with a pull request (progress.pull_request set and
          spec.repository known), it checks the pull request's state every
          PULL_REQUEST_CHECK_INTERVAL (app.delivery.models), the first check one
          interval after it began to wait, with the activity 'pull_request_state', args
          [repository full name ('owner/name'), pull request number], returning
          {'state': 'open' or 'closed', 'merged': bool}. A failed check changes nothing;
          the next interval checks again. A check that finds the pull request open keeps
          it waiting.
        - When the pull request is merged (found by the check, or by the
          `pull_request_closed` signal), the delivery stops waiting and the workflow
          completes (execution status COMPLETED), returning its progress.
        - When it is closed without being merged, the delivery stops waiting and the
          workflow ends as cancelled (execution status CANCELED).
        Either way it closes no later than one check interval after the merge or close,
        whether or not a signal arrived, and whether or not the open-review-findings flag
        (progress.open_findings) is still raised: the flag stays raised in the state
        `get_state` reports. The same holds for an onboarding's delivery
        (spec.onboarding), and for a delivery that was already waiting after its push
        before this behaviour existed, such as a run started from the state an earlier
        run waited with (`record_only`).
        - Without a merge or close, it closes on its own after TASK_IDLE_TIMEOUT."""
        ...

    def restart(self, state: DeliveryState) -> None:
        """Update: continue the waiting delivery as a new run from `state`: the user's
        retry, acceptance of open findings or instructions about them. Rejected unless
        the delivery waits."""
        ...

    def get_state(self) -> DeliveryState:
        """Query: the delivery's current state, its progress included."""
        ...

    async def mention(self, mention: Any) -> None:
        """Update: an authorised mention of the app on the delivery's pull request, a
        dict with 'comment_id', 'comment_type' ('conversation' or 'review'), 'url',
        'author', 'body', 'received_at', 'classification', 'status', 'repository'
        ('owner/name'), 'pull_number' and 'reply_to'. Recorded with the activity
        'record_new_mention' (args [session_id, mention, recording] -> bool), classified
        with 'classify_mention' (args [session_id, body] -> 'fix', 'clarification' or
        'unclear'), and answered with 'reply_to_mention' (args [full_name, number,
        comment_type, comment_id, author, text] -> reply URL), or queued as a fix round;
        'save_mention' (args [session_id, mention]) keeps how it stands."""
        ...

    def pull_request_closed(self, number: int, merged: bool) -> None:
        """Signal: GitHub reported (a pull_request webhook event) that pull request
        `number` of the delivery was closed, merged when `merged` is true. A waiting
        delivery whose pull request it is stops waiting at once: it completes when the
        pull request was merged and ends as cancelled when it was closed without being
        merged, as `run` describes. A signal about another pull request changes
        nothing."""
        ...
