"""What every mixin of DeliveryWorkflow can rely on the workflow to hold."""
from typing import TYPE_CHECKING, Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from app.delivery.models import DeliveryProgress, DeliveryState, TaskSpec


class FlowBase:
    """Declares, for the type checker alone, the attributes DeliveryWorkflow sets in its
    `__init__` and the methods its mixins call on one another."""

    state: DeliveryState
    spec: TaskSpec
    progress: DeliveryProgress
    _child_ids: dict[str, int]
    _correcting: list[dict[str, Any]]

    if TYPE_CHECKING:
        # The mixins are one workflow: a method of one that another calls is not known
        # to the checker from the mixin's own class.
        def __getattr__(self, name: str) -> Any: ...
