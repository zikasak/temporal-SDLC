"""The top-level flow of a task: its stages, what each stage hands to the next and the
task's short name. Safe to import from workflow code."""

import hashlib
from dataclasses import dataclass, field
from typing import Literal

from app.api_design.models import ApiDesignState
from app.architecture.models import ArchitectureState
from app.delivery.models import DeliveryState, TaskSpec, delivery_id
from app.github.models import RepositorySelection
from app.requirements.models import RequirementsState

WORKFLOW_TYPE = "TaskWorkflow"
TASK_FLOW_PREFIX = "task-flow-"
# The stages of a task, in the order they run.
Stage = Literal["requirements", "architecture", "api_design", "delivery"]
STAGE_WORKFLOW_TYPES: dict[str, str] = {
    "requirements": "RequirementsWorkflow",
    "architecture": "ArchitectureWorkflow",
    "api_design": "ApiDesignWorkflow",
    "delivery": "DeliveryWorkflow",
}
# pending: created, waiting for the stages it depends on; running: started; stopped: a
# delivery stopped from outside, which the user can start again; closed: ended on its
# own; skipped: will never run.
StageStatus = Literal["pending", "running", "stopped", "closed", "skipped"]
FINISHED_STAGE_STATUSES = ("closed", "skipped")
# How a fork or a fix round's task flow is named after the original task's.
FORK_SUFFIX = " (fork)"
FIX_SUFFIX = " (fix)"
NAME_CHARS = 80
# The kinds whose task goes through the architecture and the API design.
DESIGNED_KINDS = ("new_service", "api_change")


@dataclass
class StageRecord:
    stage: Stage
    workflow_id: str
    status: StageStatus = "pending"
    # How a closed stage ended: completed, cancelled or failed.
    outcome: str | None = None


@dataclass
class TaskState:
    """Workflow input and query result of a task flow."""

    kind: str | None = None
    repository: RepositorySelection | None = None
    # What needs to be done, as the intake said; what the flow is first named from.
    task: str = ""
    name: str = ""
    # new: a new requirements interview; fork: a fork of one of the task's sessions; fix:
    # a fix round on the delivery of a task whose flow closed.
    origin: Literal["new", "fork", "fix"] = "new"
    # The task flow this one was forked from or carries on.
    source_task_flow_id: str | None = None
    stages: list[StageRecord] = field(default_factory=list)
    # The earlier stages of the original task that a fork or a fix round links to.
    links: list[str] = field(default_factory=list)
    # The start of the stage that runs at once; the others start pending.
    requirements: RequirementsState | None = None
    architecture: ArchitectureState | None = None
    api_design: ApiDesignState | None = None
    delivery: DeliveryState | None = None
    # Set once the flow has closed every stage, with the status it closes with.
    outcome: str | None = None


@dataclass
class RequirementsHandOff:
    """What a finished requirements stage hands to its task flow."""

    state: RequirementsState
    # True when the user generated the document; false when it was written because the
    # session was idle for a day, or could not be written.
    finalized: bool
    requirements_text: str = ""


@dataclass
class ApiDesignHandOff:
    """What a finalized API design hands to its task flow: the delivery to start."""

    document: dict | None
    delivery: DeliveryState | None


def new_task_flow_id(token: str) -> str:
    return f"{TASK_FLOW_PREFIX}{token}"


def fix_task_flow_id(follow_up_session_id: str) -> str:
    """The task flow of the fix round run as `follow_up_session_id`: derived from it, so
    that a handover tried again finds the flow it started."""
    digest = hashlib.sha256(f"{follow_up_session_id}:task-flow".encode()).hexdigest()[:32]
    return f"{TASK_FLOW_PREFIX}{digest}"


def is_task_flow_id(workflow_id: str) -> bool:
    return workflow_id.startswith(TASK_FLOW_PREFIX)


def fallback_name(task: str) -> str:
    """The name a task flow has until the LLM names it: the start of the task."""
    text = " ".join(task.split())
    if len(text) <= NAME_CHARS:
        return text or "New task"
    return text[: NAME_CHARS - 1].rstrip() + "…"


def suffixed(name: str, suffix: str) -> str:
    """The name of a fork or a fix round's task flow; a fork of a fork keeps one suffix."""
    base = name[: -len(suffix)] if name.endswith(suffix) else name
    return (base or "Task") + suffix


def delivery_stage_id(kind: str | None, requirements_id: str, api_design_id: str | None) -> str:
    """The delivery of a task: named after the API design of a designed task and after
    the requirements session of any other."""
    if kind in DESIGNED_KINDS and api_design_id:
        return delivery_id(api_design_id)
    return delivery_id(requirements_id)


def direct_delivery(session_id: str, source: RequirementsState, requirements_text: str,
                    task_flow_id: str) -> DeliveryState | None:
    """The delivery of a bugfix or documentation task, which leaves the API as it is:
    the brief and the repository's contract, as finalized requirements session
    `session_id` read it. None without a contract to deliver against."""
    contract = source.contract
    if not contract:
        return None
    return DeliveryState(spec=TaskSpec(
        session_id=session_id,
        requirements=contract["requirements"],
        requirements_document=contract["requirements_document"],
        api=contract["api"],
        kind=source.kind,
        brief={"requirements": source.requirements,
               "requirements_document": requirements_text,
               "retired": source.retired},
        language=contract["language"],
        repository=source.repository,
        checkout=source.checkout,
        # Given to the coding agents read-only; the repository's files stay as they are.
        architecture=contract.get("architecture"),
        architecture_structure=contract.get("architecture_structure"),
    ), task_flow_id=task_flow_id)


def pending_delivery(session_id: str, kind: str | None, repository: RepositorySelection | None,
                     task_flow_id: str) -> DeliveryState:
    """The placeholder a pending delivery waits with until it begins."""
    return DeliveryState(spec=TaskSpec(session_id=session_id, requirements=[],
                                       requirements_document="", api={},  # type: ignore[typeddict-item]
                                       kind=kind, repository=repository),  # type: ignore[arg-type]
                         task_flow_id=task_flow_id, pending=True)


def stage_ids(state: TaskState) -> dict[str, list[str]]:
    """The task flow's memo of its stages: the workflow IDs by stage and its links."""
    result: dict[str, list[str]] = {}
    for record in state.stages:
        result.setdefault(record.stage, []).append(record.workflow_id)
    return {"stages": [record.workflow_id for record in state.stages],
            "delivery": result.get("delivery", []),
            "links": list(state.links)}
