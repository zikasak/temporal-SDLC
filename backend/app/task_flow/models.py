"""What a task flow holds: its stages, which it creates as soon as it knows the task needs
them and starts once the stages before them have handed the task on, its name and the
earlier stages of another task it links to. Safe to import from workflow code."""

import re
from dataclasses import dataclass, field
from typing import Literal

from app.api_design.models import ApiDesignState
from app.architecture.models import ARCHITECTURE_KINDS, ArchitectureState
from app.delivery.models import DeliveryState
from app.github.models import RepositorySelection
from app.requirements.models import DIRECT_KINDS, Requirement, RequirementsState, TaskKind

WORKFLOW_TYPE = "TaskWorkflow"
TASK_FLOW_PREFIX = "task-flow-"
TASK_FLOW_ID_PATTERN = re.compile(r"task-flow-[0-9a-f]{32}")

Stage = Literal["requirements", "architecture", "api_design", "delivery"]
STAGE_WORKFLOWS: dict[str, str] = {
    "requirements": "RequirementsWorkflow",
    "architecture": "ArchitectureWorkflow",
    "api_design": "ApiDesignWorkflow",
    "delivery": "DeliveryWorkflow",
}
STAGE_PREFIXES: dict[str, str] = {
    "requirements": "requirements-session-",
    "architecture": "architecture-session-",
    "api_design": "api-design-session-",
}
# A stage that is created but not started yet, or will never run, and how a started
# stage ended: completed, cancelled with its flow, or stopped from outside (failed).
ChildStatus = Literal["pending", "running", "completed", "cancelled", "skipped", "failed"]
CLOSED_CHILD_STATUSES = ("completed", "cancelled", "skipped", "failed")
Origin = Literal["new", "fork", "fix"]

FORK_SUFFIX = " (fork)"
FIX_SUFFIX = " (fix)"
# How long a name may be, and how much of the task its fallback takes.
NAME_CHARS = 80
FALLBACK_NAME_CHARS = 60


def stages_of(kind: TaskKind | None) -> tuple[str, ...]:
    """The stages a task of `kind` needs, in order."""
    if kind in ARCHITECTURE_KINDS:
        return ("requirements", "architecture", "api_design", "delivery")
    if kind in DIRECT_KINDS:
        return ("requirements", "delivery")
    # The kind is not known yet: only the interview.
    return ("requirements",)


def fallback_name(text: str) -> str:
    """A name taken from the task itself, when the LLM cannot write one."""
    words = " ".join(str(text or "").split())
    if not words:
        return "Task"
    if len(words) <= FALLBACK_NAME_CHARS:
        return words
    cut = words[:FALLBACK_NAME_CHARS].rsplit(" ", 1)[0] or words[:FALLBACK_NAME_CHARS]
    return cut.rstrip(" ,.;:-") + "…"


def clean_name(text: str) -> str:
    """One line of at most NAME_CHARS characters, without quotes or a label."""
    line = next((item for item in str(text or "").splitlines() if item.strip()), "")
    line = " ".join(line.split()).strip().strip("\"'`*#").strip()
    line = re.sub(r"^(name|title)\s*:\s*", "", line, flags=re.IGNORECASE).strip("\"'` ")
    if len(line) > NAME_CHARS:
        line = line[:NAME_CHARS].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"
    return line


@dataclass
class StageChild:
    """One stage of the task flow, as the task flow follows it."""

    stage: str
    workflow_id: str
    status: ChildStatus = "pending"
    # What the stage last reported: whether it waits for the user, and whether its
    # delivery has unresolved review findings (None for a stage without a delivery).
    waiting: bool = False
    unresolved: bool | None = None
    # Whether the stage handed the task on to the next one, or ended without.
    handed_off: bool = False


@dataclass
class RequirementsHandoff:
    """What a requirements session of a task flow hands on once it ended: `finalized`
    when the user generated the document (not when an idle session wrote it), and then
    the start of the architecture session of a new service or an API change on a
    repository, or the delivery of a bugfix or a documentation change."""

    finalized: bool = False
    kind: TaskKind | None = None
    requirements: list[Requirement] = field(default_factory=list)
    architecture: ArchitectureState | None = None
    delivery: DeliveryState | None = None


@dataclass
class TaskFlowState:
    """Workflow input of a task flow, and its query result."""

    kind: TaskKind | None = None
    repository: RepositorySelection | None = None
    # What needs to be done, as the intake (or the original task) said; the first name
    # is written from it.
    task: str = ""
    # The current short name; empty until written. A fork or fix round starts with the
    # original's name and its suffix.
    name: str = ""
    name_suffix: str = ""
    origin: Origin = "new"
    # The stage the flow starts working with, its workflow ID and its start: a new task
    # and a fork of a requirements session begin with the requirements, a fork of a later
    # stage with that stage, and a fix round with its delivery.
    first_stage: str = "requirements"
    first_stage_id: str = ""
    requirements: RequirementsState | None = None
    architecture: ArchitectureState | None = None
    design: ApiDesignState | None = None
    delivery: DeliveryState | None = None
    # The flow's own stages, in the order they were created.
    children: list[StageChild] = field(default_factory=list)
    # The delivery whose outcome the flow closes with.
    delivery_workflow_id: str | None = None
    # Earlier stages of another task this forked or fix-round flow links to, and those of
    # them that have closed.
    linked: list[str] = field(default_factory=list)
    linked_closed: list[str] = field(default_factory=list)
    # Whether the name was rewritten from the finalized requirements.
    renamed: bool = False
