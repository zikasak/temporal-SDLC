"""How long a Chat answer may take, and how its agent keeps within it.

The answering activity heartbeats after each model request, so a worker that died or a
model call that hangs is found within CHAT_ANSWER_HEARTBEAT, while a slow but working
agent may run up to CHAT_ANSWER_TIMEOUT. When the activity's deadline comes within
DEADLINE_WARNING_HEARTBEATS heartbeat timeouts, the model is told once to finish."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from temporalio import activity

CHAT_ANSWER_TIMEOUT = timedelta(minutes=100)
# Above the longest single model request seen (about 1.5 minutes on a local model).
CHAT_ANSWER_HEARTBEAT = timedelta(minutes=6)
# How many heartbeat timeouts before the deadline the model is told that time is up.
DEADLINE_WARNING_HEARTBEATS = 2
TIME_IS_UP = (
    "[System] The time for this request is nearly up: about {minutes} minutes remain "
    "before it is cancelled. Do not start new investigations. Write your final answer "
    "now from what you have found so far, and say what you could not check."
)


def _deadline(info: activity.Info) -> datetime | None:
    """When the server cancels this attempt: the earlier of its start-to-close and
    schedule-to-close timeouts."""
    deadlines = []
    if info.start_to_close_timeout:
        deadlines.append(info.started_time + info.start_to_close_timeout)
    if info.schedule_to_close_timeout:
        deadlines.append(info.scheduled_time + info.schedule_to_close_timeout)
    return min(deadlines, default=None)


def model_request_pacer(
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Callable[[], str | None]:
    """What the agent calls after each model request in the running activity: it
    heartbeats, and returns the time-is-up notice once the deadline is near (only once).
    An activity without a heartbeat timeout only heartbeats."""
    info = activity.info()
    deadline = _deadline(info)
    warn_within = (info.heartbeat_timeout * DEADLINE_WARNING_HEARTBEATS
                   if info.heartbeat_timeout else None)
    requests = 0
    warned = False

    def after_model_request() -> str | None:
        nonlocal requests, warned
        requests += 1
        activity.heartbeat(f"model request {requests}")
        if warned or deadline is None or warn_within is None:
            return None
        remaining = deadline - now()
        if remaining > warn_within:
            return None
        warned = True
        activity.logger.warning("Telling the model to finish: %s left before the deadline",
                                remaining)
        minutes = max(0, int(remaining.total_seconds() // 60))
        return TIME_IS_UP.format(minutes=minutes)

    return after_model_request
