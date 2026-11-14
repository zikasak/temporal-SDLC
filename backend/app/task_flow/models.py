"""The top-level flow of one task (TaskWorkflow): its stages, created up front per task
kind, and what it knows of each. Safe to import from workflow code."""

from dataclasses import dataclass, field
from typing import Any, Literal

from app.api_design.models import ApiDesignState
from app.architecture.models import ArchitectureState
from app.delivery.models import DeliveryState
from app.requirements.models import RequirementsState

WORKFLOW_TYPE = "TaskWorkflow"
TASK_FLOW_PREFIX = "task-flow-"
FORK_SUFFIX = " (fork)"
FIX_SUFFIX = " (fix)"

Stage = Literal["requirements", "architecture", "api_design", "delivery"]
STAGE_TYPES: dict[str, str] = {
    "requirements": "RequirementsWorkflow",
    "architecture": "ArchitectureWorkflow",
    "api_design": "ApiDesignWorkflow",
    "delivery": "DeliveryWorkflow",
}
# The stages a task flow creates up front for each kind of task.
KIND_STAGES: dict[str, tuple[str, ...]] = {
    "new_service": ("requirements", "architecture", "api_design", "delivery"),
    "api_change": ("requirements", "architecture", "api_design", "delivery"),
    "bugfix": ("requirements", "delivery"),
    "docs": ("requirements", "delivery"),
}
STAGE_ORDER = ("requirements", "architecture", "api_design", "delivery")
# What a child of a task flow is: created and waiting (pending), working (running), never
# to run (skipped), or closed with its own outcome.
ChildStatus = Literal["pending", "running", "skipped", "completed", "cancelled", "stopped"]
OPEN_CHILD = ("pending", "running")


def stages_for(kind: str | None) -> tuple[str, ...]:
    return KIND_STAGES.get(kind or "", KIND_STAGES["new_service"])


@dataclass
class TaskChild:
    stage: str
    workflow_id: str
    status: str = "pending"
    # Whether the stage handed the task on to the next one.
    handed: bool = False
    waiting: bool = False
    unresolved: bool | None = None


@dataclass
class TaskFlowState:
    """Workflow input and query result. A new task starts with its requirements session;
    a fork with the forked session, linking to the original task's earlier stages; a fix
    round on a closed task's delivery with that delivery, linking to all of the original
    task's stages."""

    kind: str | None = None
    repository: dict[str, Any] | None = None
    # What the intake said needs to be done, which the first name is written from.
    task: str = ""
    name: str | None = None
    # None for a new task, "fork" or "fix".
    origin: str | None = None
    source_task_flow_id: str | None = None
    # The stage the flow starts working at once, and its start; the stages after it are
    # created as pending.
    first_stage: str = "requirements"
    requirements: RequirementsState | None = None
    architecture: ArchitectureState | None = None
    design: ApiDesignState | None = None
    delivery: DeliveryState | None = None
    # The workflow ID of the first stage.
    first_id: str = ""
    # The requirements session of the task: its own child, or the linked one of a fork.
    requirements_session_id: str | None = None
    children: list[TaskChild] = field(default_factory=list)
    # The earlier stages of the original task this flow links to, and those still open.
    linked: list[str] = field(default_factory=list)
    linked_open: list[str] = field(default_factory=list)
    # How the task ended: its delivery's status, cancelled when it was skipped.
    outcome: str | None = None
