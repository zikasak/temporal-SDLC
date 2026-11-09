from temporalio.client import Client

from app.requirements.models import RequirementsState


async def connect_temporal() -> Client:
    """The Temporal client this module reaches sessions through; besides it, its functions
    reach Temporal only through app.temporal.sessions (with that module's
    connect_temporal) and record sessions only through `register`."""
    ...


async def register(workflow_id: str) -> None:
    """Record a session the API started (app.temporal.registry.register)."""
    ...


async def get_state(session_id: str) -> RequirementsState:
    """The session's state, from the `get_state` query of its "RequirementsWorkflow"
    (workflow ID `session_id`)."""
    ...


async def fork_session(session_id: str, message_index: int) -> str:
    """A new requirements session ("RequirementsWorkflow", started on the API's task
    queue) with the conversation up to message `message_index` and the requirements as
    they were at that point; returns its ID.

    A requirements session is the root of its flow. Forking is allowed after any message
    while the flow has not completed or been cancelled, that is while the session's
    workflow runs. Once it has completed or been cancelled (execution status COMPLETED or
    CANCELED), such as after its delivery's pull request was merged or closed, the fork
    is refused with SessionBusy (app.temporal.sessions; answered 409) and no session is
    started."""
    ...
