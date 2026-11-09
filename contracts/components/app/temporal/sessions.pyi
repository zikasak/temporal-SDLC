from typing import Any

from temporalio.client import Client, WorkflowUpdateStage


async def connect_temporal() -> Client:
    """The Temporal client this module's functions reach sessions through."""
    ...


class SessionBusy(Exception):
    """The session cannot do this now; the API answers 409."""


class SessionEnded(SessionBusy):
    """The session's workflow has closed."""


async def start_update(session_id: str, update: str, *args: Any,
                       wait_for_stage: WorkflowUpdateStage = ...,
                       update_id: str | None = None) -> None:
    """Send the update `update` with `args` to the workflow `session_id`; SessionBusy when
    its validator rejects it, SessionEnded when the workflow has closed."""
    ...


async def is_running(workflow_id: str) -> bool:
    """Whether the workflow's latest run is open; false when there is none."""
    ...
