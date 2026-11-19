"""The top-level flow of a task (TaskWorkflow) and what its stages hand to it. Safe to
import from workflow code."""

from dataclasses import dataclass, field
from typing import Any, Literal

from app.api_design.models import ApiDesignState
from app.architecture.models import ArchitectureState
from app.delivery.models import DeliveryState
from app.github.models import RepositorySelection
from app.requirements.models import Requirement, RequirementsState, TaskKind

TASK_FLOW_TYPE = "TaskWorkflow"
TASK_FLOW_PREFIX = "task-flow-"
# The longest name a task flow is listed with.
NAME_CHARS = 80
FORK_SUFFIX = " (fork)"
FIX_SUFFIX = " (fix)"

Stage = Literal["requirements", "architecture", "api_design", "delivery"]
STAGES: tuple[Stage, ...] = ("requirements", "architecture", "api_design", "delivery")
# The kinds of task whose stages include the architecture and the API design.
DESIGNED_KINDS = ("new_service", "api_change")
Origin = Literal["new", "fork", "fix"]


def new_task_flow_id(hex_id: str) -> str:
    return f"{TASK_FLOW_PREFIX}{hex_id}"


def fallback_name(text: str) -> str:
    """A name from the task's own words, when the LLM cannot write one."""
    words = " ".join(str(text or "").split())
    if not words:
        return "Task"
    if len(words) <= NAME_CHARS:
        return words
    return words[:NAME_CHARS - 1].rstrip() + "…"


def bounded_name(name: str, suffix: str = "") -> str:
    """`name` with `suffix`, shortened so that both fit NAME_CHARS."""
    name = " ".join(str(name or "").split()) or "Task"
    room = NAME_CHARS - len(suffix)
    if len(name) > room:
        name = name[:room - 1].rstrip() + "…"
    return name + suffix


@dataclass
class RequirementsResult:
    """What a requirements stage hands to its task flow as it closes."""

    # Whether the user generated the document; false when it was written because the
    # session was idle for a day, or when the session failed.
    finalized: bool = False
    kind: TaskKind | None = None
    requirements: list[Requirement] = field(default_factory=list)
    document: dict[str, Any] | None = None
    # The start of the next stage: the architecture of a new service or an API change, or
    # the delivery of a bugfix or documentation task.
    architecture: ArchitectureState | None = None
    delivery: DeliveryState | None = None


@dataclass
class DesignResult:
    """What an API design stage hands to its task flow as it closes: its document and,
    once finalized, the delivery to start."""

    document: dict[str, Any] | None = None
    delivery: DeliveryState | None = None


@dataclass
class TaskFlowState:
    """Workflow input and query result of a TaskWorkflow."""

    kind: TaskKind | None = None
    repository: RepositorySelection | None = None
    # What needs to be done, as the intake said it: what the first name is written from.
    task: str = ""
    # The current short name; written by the LLM at start unless given.
    name: str = ""
    origin: Origin = "new"
    # The IDs the task flow reserved for its stages, whether or not created.
    requirements_session_id: str | None = None
    architecture_session_id: str | None = None
    api_design_session_id: str | None = None
    # The session the delivery is named after (delivery-<session>).
    delivery_session_id: str | None = None
    # The start of the first stage that works at once; the stages before it are linked
    # earlier stages of another task (a fork's or a fix round's).
    requirements: RequirementsState | None = None
    architecture: ArchitectureState | None = None
    api_design: ApiDesignState | None = None
    delivery: DeliveryState | None = None
    # Earlier stages of another task this task flow links to.
    linked: list[str] = field(default_factory=list)
    # The stages created as children, by stage, and those that started working, were
    # skipped or closed.
    created: dict[str, str] = field(default_factory=dict)
    started: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    # How the delivery ended: completed, cancelled, skipped; None while it has not.
    delivery_outcome: str | None = None
    error: str | None = None


def stages_for(kind: TaskKind | None) -> tuple[Stage, ...]:
    """The stages a task of `kind` has."""
    if kind in DESIGNED_KINDS:
        return STAGES
    return ("requirements", "delivery")
