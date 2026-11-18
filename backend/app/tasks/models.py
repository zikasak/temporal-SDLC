"""The top-level flow of a task (TaskWorkflow) and its stages. Safe to import from
workflow code.

Every new requirements interview, every fork of a requirements, architecture or API
design session and every fix round on a closed task's delivery is a task flow of its own.
Its stages (requirements, architecture, API design and delivery, as the kind of task
needs them) are all its direct children, created at once: the first one works at once,
each later one waits pending until the stage before it hands the task on, and closes
skipped when that stage ended without doing so. A fork or fix round links to the earlier
stages of the task it came from, which stay children of their own task flow."""

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from app.api_design.models import ApiDesignState, empty_api
from app.architecture.models import ARCHITECTURE_KINDS, ArchitectureState
from app.delivery.models import DeliveryState, TaskSpec, delivery_id
from app.github.models import RepositorySelection
from app.requirements.models import Requirement, RequirementsState

TASK_FLOW_PREFIX = "task-flow-"
WORKFLOW_TYPE = "TaskWorkflow"
# How long a task flow's name may be, and the suffixes of the flows a fork or a fix round
# of a closed delivery starts.
NAME_CHARS = 80
FORK_SUFFIX = " (fork)"
FIX_SUFFIX = " (fix)"
DEFAULT_NAME = "New task"

StageKind = Literal["requirements", "architecture", "api_design", "delivery"]
STAGE_TYPES: dict[str, str] = {
    "requirements": "RequirementsWorkflow",
    "architecture": "ArchitectureWorkflow",
    "api_design": "ApiDesignWorkflow",
    "delivery": "DeliveryWorkflow",
}
STAGE_ORDER: tuple[StageKind, ...] = ("requirements", "architecture", "api_design", "delivery")
Origin = Literal["new", "fork", "fix"]


@dataclass
class TaskStage:
    """A stage of the task flow: pending until the task flow starts it, then running
    until it closes with its outcome (completed, cancelled, failed or skipped)."""

    kind: StageKind
    workflow_id: str
    state: Literal["pending", "running", "closed"] = "pending"
    outcome: str | None = None
    # Set for a stage created for the kind of task the intake named, which the finalized
    # requirements made another stage take the place of; it closes skipped.
    replaced: bool = False


@dataclass
class TaskHandoff:
    """What a finalized requirements session hands its task flow: the requirements the
    flow is named from, and the start of the stage that follows, if any, with the
    architecture session's ID the requirements session reports."""

    requirements: list[Requirement] = field(default_factory=list)
    architecture: ArchitectureState | None = None
    architecture_session_id: str | None = None
    delivery: DeliveryState | None = None


@dataclass
class TaskFlowState:
    """Workflow input and query result."""

    kind: str
    repository: RepositorySelection | None = None
    # The current short name; empty until named. `task` is what the LLM names it from at
    # start when no name was given, and `suffix` what a fork or fix round appends.
    name: str = ""
    task: str = ""
    suffix: str = ""
    origin: Origin = "new"
    # The task flow a fork or fix round came from, if any.
    source_task_flow_id: str | None = None
    # The session IDs of the stages, reserved when the flow starts; None for a stage the
    # task does not have or that is linked from the task it came from.
    requirements_session_id: str | None = None
    architecture_session_id: str | None = None
    api_design_session_id: str | None = None
    # The session the delivery is named after: the API design's, or the requirements
    # session's for a bugfix or documentation change.
    delivery_session_id: str | None = None
    # The start of the first stage, which works at once; every later one is pending.
    requirements: RequirementsState | None = None
    architecture: ArchitectureState | None = None
    design: ApiDesignState | None = None
    delivery: DeliveryState | None = None
    # The earlier stages of another task that this fork or fix round links to.
    linked: list[str] = field(default_factory=list)
    stages: list[TaskStage] = field(default_factory=list)
    # running until every stage closed; then completed or cancelled with its delivery.
    status: str = "running"


def new_task_flow_id() -> str:
    return f"{TASK_FLOW_PREFIX}{uuid.uuid4().hex}"


def fallback_name(text: str) -> str:
    """A name from the start of `text`, used until the LLM named the flow."""
    words = " ".join(str(text or "").split())
    if not words:
        return DEFAULT_NAME
    if len(words) <= NAME_CHARS:
        return words
    return words[: NAME_CHARS - 1].rstrip() + "…"


def with_suffix(name: str, suffix: str) -> str:
    name = " ".join(str(name or "").split()) or DEFAULT_NAME
    if suffix and not name.endswith(suffix):
        name = name[: max(1, NAME_CHARS - len(suffix))].rstrip() + suffix
    return name


def stage_kinds(kind: str | None) -> list[StageKind]:
    """The stages a task of `kind` has, in order."""
    if kind in ARCHITECTURE_KINDS:
        return ["requirements", "architecture", "api_design", "delivery"]
    return ["requirements", "delivery"]


def plan_stages(state: TaskFlowState, first: StageKind) -> TaskFlowState:
    """Reserve the IDs of the stages from `first` on, the stages before it being linked
    from the task the flow came from, and list them. The delivery is named after the API
    design, or after the requirements session of a task without one."""
    kinds = stage_kinds(state.kind)
    own = kinds[kinds.index(first):]
    if "requirements" in own and not state.requirements_session_id:
        state.requirements_session_id = f"requirements-session-{uuid.uuid4().hex}"
    if "architecture" in own and not state.architecture_session_id:
        state.architecture_session_id = f"architecture-session-{uuid.uuid4().hex}"
    if "api_design" in own and not state.api_design_session_id:
        state.api_design_session_id = f"api-design-session-{uuid.uuid4().hex}"
    if not state.delivery_session_id:
        state.delivery_session_id = (state.api_design_session_id if "api_design" in kinds
                                     else state.requirements_session_id)
    ids = {"requirements": state.requirements_session_id,
           "architecture": state.architecture_session_id,
           "api_design": state.api_design_session_id,
           "delivery": delivery_id(state.delivery_session_id or "")}
    state.stages = [TaskStage(kind=kind, workflow_id=str(ids[kind])) for kind in own]
    return state


def stage_ids(state: TaskFlowState) -> list[str]:
    return [stage.workflow_id for stage in state.stages]


def placeholder(kind: StageKind, state: TaskFlowState) -> Any:
    """The input of a pending stage: it only waits for its real start."""
    if kind == "architecture":
        return ArchitectureState(pending=True, repository=state.repository,
                                 task_flow_id=None, kind=_architecture_kind(state.kind))
    if kind == "api_design":
        return ApiDesignState(pending=True, repository=state.repository)
    if kind == "delivery":
        return DeliveryState(spec=TaskSpec(
            session_id=state.delivery_session_id or "", requirements=[],
            requirements_document="", api=empty_api(), kind=state.kind,  # type: ignore[arg-type]
            repository=state.repository), pending=True)
    raise ValueError(f"A {kind} stage is never pending.")


def _architecture_kind(kind: str) -> Any:
    return "api_change" if kind == "api_change" else "new_service"
