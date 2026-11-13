"""The task flow's input and state. Safe to import from workflow code."""

from dataclasses import dataclass, field
from typing import Any, Literal

from app.api_design.models import ApiDesignState
from app.architecture.models import ARCHITECTURE_KINDS, ArchitectureState
from app.delivery.models import DeliveryState
from app.github.models import RepositorySelection
from app.requirements.models import RequirementsState, TaskKind

WORKFLOW_TYPE = "TaskWorkflow"
ID_PREFIX = "task-flow-"
# Workflow IDs of the task flows and of the sessions they create.
REQUIREMENTS_PREFIX = "requirements-session-"
ARCHITECTURE_PREFIX = "architecture-session-"
API_DESIGN_PREFIX = "api-design-session-"

Stage = Literal["requirements", "architecture", "api_design", "delivery"]
STAGES: tuple[Stage, ...] = ("requirements", "architecture", "api_design", "delivery")
STAGE_TYPES: dict[str, str] = {
    "requirements": "RequirementsWorkflow",
    "architecture": "ArchitectureWorkflow",
    "api_design": "ApiDesignWorkflow",
    "delivery": "DeliveryWorkflow",
}
# How a stage child stands: created and waiting for the stages before it, running, closed
# (with its outcome in `outcome`), or skipped because it will never run.
StageStatus = Literal["pending", "running", "closed", "skipped"]
# The suffixes of the names of task flows a fork or a fix round starts.
FORK_SUFFIX = " (fork)"
FIX_SUFFIX = " (fix)"
NAME_CHARS = 80


def stages_of(kind: TaskKind | None) -> tuple[Stage, ...]:
    """The stages a task of `kind` goes through, in order."""
    if kind in ARCHITECTURE_KINDS:
        return STAGES
    return ("requirements", "delivery")


def fallback_name(task: str) -> str:
    """A name of the task's own words, until the LLM wrote one or when it could not."""
    text = " ".join(str(task or "").split())
    if not text:
        return "Task"
    return text if len(text) <= NAME_CHARS else text[:NAME_CHARS - 1].rstrip() + "…"


def with_suffix(name: str, suffix: str) -> str:
    """The name of a fork's or a fix round's task flow: the original's, marked once."""
    if not suffix or name.endswith(suffix):
        return name
    return name + suffix


@dataclass
class TaskStageRecord:
    """One child of the task flow: its stage, workflow ID and how it stands."""

    stage: Stage
    workflow_id: str
    status: StageStatus = "pending"
    # How a closed child ended: completed, cancelled or failed.
    outcome: str | None = None


@dataclass
class LinkedStage:
    """An earlier stage of another task that a forked or fix-round task flow links to."""

    stage: Stage
    workflow_id: str


@dataclass
class TaskFlowState:
    """The task flow's input, carried in its state and returned by its query."""

    kind: TaskKind | None = None
    repository: RepositorySelection | None = None
    # What the intake said needs to be done, which the first name is written from.
    task: str = ""
    # The current name: written by the LLM from `task` and again from the finalized
    # requirements; a fork's or a fix round's is the original's with `suffix`.
    name: str = ""
    suffix: str = ""
    # The stage the flow starts with, and its state: a new task starts with its
    # requirements, a fork with the forked session, a fix round with its delivery.
    first_stage: Stage = "requirements"
    first_session_id: str = ""
    requirements: RequirementsState | None = None
    architecture: ArchitectureState | None = None
    api_design: ApiDesignState | None = None
    delivery: DeliveryState | None = None
    # The session whose ID names the first stage's delivery, when it is the delivery.
    delivery_session_id: str | None = None
    # The other task's earlier stages a fork or a fix round links to.
    linked: list[LinkedStage] = field(default_factory=list)
    # The children, in the order they were created.
    stages: list[TaskStageRecord] = field(default_factory=list)
    # The sessions of the task the flow started, as the session responses report them.
    requirements_session_id: str | None = None
    architecture_session_id: str | None = None
    api_design_session_id: str | None = None
    # Set once the first children were created, which the start of the flow waits for.
    created: bool = False
    # How the flow closed: the status of its delivery child, or cancelled without one.
    outcome: str | None = None


def stage_record(state: TaskFlowState, stage: str) -> TaskStageRecord | None:
    """The latest child of the stage, if the flow has one."""
    found = [item for item in state.stages if item.stage == stage]
    return found[-1] if found else None


def started_id(state: TaskFlowState, stage: str) -> str | None:
    """The ID of the stage's child once it has begun: none while pending or skipped."""
    record = stage_record(state, stage)
    if record is None or record.status in ("pending", "skipped"):
        return None
    return record.workflow_id


def linked_ids(state: Any) -> list[str]:
    return [item.workflow_id for item in state.linked]


def as_dict(value: Any) -> Any:
    """A result decoded without its type, as a dict."""
    return value if isinstance(value, dict) else {}
