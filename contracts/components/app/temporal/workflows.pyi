from typing import Any

from app.api_design.models import ApiDesignState
from app.delivery.models import DeliveryState
from app.requirements.models import RequirementsState


class RequirementsWorkflow:
    """One requirements session, registered under the workflow name
    "RequirementsWorkflow": the root of its flow. Its children, started by their
    registered names on the session's own task queue, are its architecture session
    ("ArchitectureWorkflow"), its API designs ("ApiDesignWorkflow") and, for a 'bugfix'
    or 'docs' task, its delivery ("DeliveryWorkflow", workflow ID
    'delivery-<session ID>', argument a DeliveryState). Its activities, by registered
    name on the same task queue: 'write_requirements_document' (args [session_id,
    messages, requirements, retired] -> the document {'document_id', ...}),
    'read_requirements_document' (args [session_id, document_id] -> its text),
    'register_session' (args [session_id] -> None), 'requirements_turn' (an LLM turn) and,
    for 'api_change', 'bugfix' and 'docs' tasks, 'read_repository_contract'."""

    def __init__(self, state: RequirementsState | None = None) -> None:
        """The session from `state`; a state with a repository but no checkout checks it
        out first."""
        ...

    async def run(self, state: RequirementsState | None = None) -> dict[str, Any] | None:
        """Interview until the `finalize` update writes the document, then hand the task
        on: a 'new_service' or 'api_change' task to its architecture session, the child
        "ArchitectureWorkflow" (argument an ArchitectureState; its result, an
        ArchitectureResult or None, names the API design to start next), whose API design
        it then starts as its child "ApiDesignWorkflow" (argument the ApiDesignState of
        the result); a 'bugfix' or 'docs' task to its delivery, the child
        "DeliveryWorkflow".

        The session is open while any part of its flow is. Once its architecture
        session, its API designs and its delivery have all ended, the session ends too,
        at once rather than waiting further (well within a day): it completes (execution
        status COMPLETED), returning its document, when the flow's delivery completed,
        such as because its pull request was merged; it ends as cancelled (execution
        status CANCELED) when the flow's delivery was cancelled because its pull request
        was closed without being merged, that is when its API design ("ApiDesignWorkflow")
        or, for a 'bugfix' or 'docs' task, its delivery ended cancelled."""
        ...

    async def finalize(self, force: bool = False) -> None:
        """Update: write the requirements document; rejected unless the session is
        'interviewing' with a `kind`, recorded requirements and an assistant message
        last. For a task, its requirements review clears first, unless `force` continues
        despite it."""
        ...

    def get_state(self) -> RequirementsState:
        """Query: the session's state."""
        ...


class ApiDesignWorkflow:
    """One API design session, registered under the workflow name "ApiDesignWorkflow",
    normally a child of its requirements session. Once its document is finalized it
    delivers the task with its child "DeliveryWorkflow" (workflow ID
    'delivery-<session ID>', argument a DeliveryState, on the session's own task
    queue)."""

    def __init__(self, state: ApiDesignState) -> None:
        """The session from `state`. A session with no messages first drafts the API with
        an LLM turn; one with status 'completed' and a document goes straight on to its
        delivery."""
        ...

    async def run(self, state: ApiDesignState) -> dict[str, Any] | None:
        """Discuss the API until it is finalized, then run the delivery and end with it:
        when the delivery completed, such as because its pull request was merged, the
        session completes (execution status COMPLETED), returning its document; when the
        delivery was cancelled because its pull request was closed without being merged
        (the child ended with status CANCELED), the session ends as cancelled too
        (execution status CANCELED), at once. Only a delivery stopped otherwise, such as
        a terminated one, keeps the session open for a while for it to be started
        again."""
        ...

    def get_state(self) -> ApiDesignState:
        """Query: the session's state."""
        ...

    def restart_delivery(self, state: DeliveryState) -> None:
        """Update: start this session's stopped delivery again from `state`."""
        ...
