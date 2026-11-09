from temporalio.client import Client

from app.architecture.models import ArchitectureState


async def connect_temporal() -> Client:
    """The Temporal client this module reaches sessions through; besides it, its functions
    reach Temporal only through app.temporal.sessions (with that module's
    connect_temporal) and record sessions only through `register`."""
    ...


async def register(workflow_id: str) -> None:
    """Record a session the API started (app.temporal.registry.register)."""
    ...


async def get_state(session_id: str) -> ArchitectureState:
    """The session's state, from the `get_state` query of its "ArchitectureWorkflow"
    (workflow ID `session_id`)."""
    ...


async def fork_session(session_id: str, message_index: int) -> str:
    """A new architecture session of the same requirements session with the conversation
    up to message `message_index`; returns its ID.

    Refused with SessionBusy (app.temporal.sessions; answered 409), starting nothing:
    - once this session's API design has started (its state's api_design_session_id is
      set);
    - once its flow has completed or been cancelled, that is once the workflow of its
      requirements session (the state's source_session_id, the root of the flow) has
      completed or been cancelled (execution status COMPLETED or CANCELED), such as after
      the delivery's pull request was merged or closed. The same rule refuses forks of
      requirements and API design sessions of a closed flow."""
    ...
