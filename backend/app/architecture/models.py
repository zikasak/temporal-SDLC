"""The architecture of a task: its components, the data flow between them and the key
technology choices, as contracts/architecture.json holds it and contracts/architecture.md
renders it. Safe to import from workflow code."""

import json
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict

from app.api_design.models import ApiDesignState
from app.conversations.models import ConversationMessage, ForkOrigin
from app.github.models import RepositoryCheckout, RepositorySelection
from app.requirements.models import Requirement

MARKDOWN_FILENAME = "architecture.md"
JSON_FILENAME = "architecture.json"
# The kinds of task that go through the architecture stage; only an API change may skip it.
ARCHITECTURE_KINDS = ("new_service", "api_change")
SKIPPABLE_KINDS = ("api_change",)
# The key an assistant message carries when it proposed skipping the stage.
SKIP_PROPOSAL_KEY = "skip_proposal"

ArchitectureStatus = Literal["preparing", "drafting", "interviewing", "turn_running",
                             "finalizing", "completed", "skipped", "failed"]
# An architecture session in one of these is over.
FINISHED_STATUSES = ("completed", "skipped", "failed")

NOT_DRAFTED = "The architecture has not been drafted yet."
NO_COMPONENTS = "The architecture has no components yet."
NO_DATA_FLOWS = "The architecture records no data flow between its components yet."
NO_TECHNOLOGY = "The architecture records no technology choice yet."


class ArchitectureComponent(TypedDict):
    name: str
    responsibilities: list[str]


class ArchitectureDataFlow(TypedDict):
    source: str
    target: str
    description: str


class ArchitectureTechnologyChoice(TypedDict):
    topic: str
    choice: str
    rationale: NotRequired[str]


class ArchitectureStructure(TypedDict):
    components: list[ArchitectureComponent]
    data_flows: list[ArchitectureDataFlow]
    technology_choices: list[ArchitectureTechnologyChoice]


class SkipProposal(TypedDict):
    reason: str
    message_index: int


def empty_structure() -> ArchitectureStructure:
    return {"components": [], "data_flows": [], "technology_choices": []}


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize_structure(value: Any) -> ArchitectureStructure:
    """The structure `value` holds, as written to architecture.json; raises ValueError or
    TypeError for anything that is not one."""
    if not isinstance(value, dict):
        raise TypeError("The architecture is not an object.")
    components: list[ArchitectureComponent] = []
    for item in value.get("components") or []:
        name = _text(item["name"])
        responsibilities = [_text(entry) for entry in item.get("responsibilities") or []
                            if _text(entry)]
        if not name:
            raise ValueError("A component has no name.")
        components.append({"name": name, "responsibilities": responsibilities})
    flows: list[ArchitectureDataFlow] = []
    for item in value.get("data_flows") or []:
        flow = {"source": _text(item["source"]), "target": _text(item["target"]),
                "description": _text(item.get("description"))}
        if not flow["source"] or not flow["target"]:
            raise ValueError("A data flow has no source or target.")
        flows.append(flow)  # type: ignore[arg-type]
    choices: list[ArchitectureTechnologyChoice] = []
    for item in value.get("technology_choices") or []:
        choice: ArchitectureTechnologyChoice = {"topic": _text(item["topic"]),
                                                "choice": _text(item["choice"])}
        if not choice["topic"] or not choice["choice"]:
            raise ValueError("A technology choice has no topic or choice.")
        if _text(item.get("rationale")):
            choice["rationale"] = _text(item.get("rationale"))
        choices.append(choice)
    return {"components": components, "data_flows": flows, "technology_choices": choices}


def architecture_problems(structure: ArchitectureStructure | None) -> list[str]:
    """What must be settled before the architecture can be finalized."""
    if structure is None:
        return [NOT_DRAFTED]
    problems = []
    names = {item["name"] for item in structure["components"]}
    if not structure["components"]:
        problems.append(NO_COMPONENTS)
    for item in structure["components"]:
        if not item["responsibilities"]:
            problems.append(f"Component {item['name']} has no responsibilities.")
    if len(structure["components"]) > 1 and not structure["data_flows"]:
        problems.append(NO_DATA_FLOWS)
    for item in structure["data_flows"]:
        for end in ("source", "target"):
            if item[end] not in names:
                problems.append(f"The data flow from {item['source']} to {item['target']} "
                                f"names an unknown component {item[end]}.")
        if not item["description"]:
            problems.append(f"The data flow from {item['source']} to {item['target']} "
                            "does not say what data flows.")
    if not structure["technology_choices"]:
        problems.append(NO_TECHNOLOGY)
    return problems


def render_architecture_markdown(structure: ArchitectureStructure) -> str:
    """architecture.md, with its fixed headings."""
    lines = ["# Architecture", "", "## Components", ""]
    if not structure["components"]:
        lines += ["None recorded.", ""]
    for item in structure["components"]:
        lines += [f"### {item['name']}", ""]
        lines += [f"- {entry}" for entry in item["responsibilities"]] or ["- (none)"]
        lines.append("")
    lines += ["## Data flow", ""]
    if not structure["data_flows"]:
        lines += ["None recorded.", ""]
    else:
        lines += [f"- **{item['source']} → {item['target']}**: {item['description']}"
                  for item in structure["data_flows"]]
        lines.append("")
    lines += ["## Technology choices", ""]
    if not structure["technology_choices"]:
        lines += ["None recorded.", ""]
    else:
        lines += ["| Topic | Choice | Rationale |", "| --- | --- | --- |"]
        lines += [f"| {_cell(item['topic'])} | {_cell(item['choice'])} | "
                  f"{_cell(item.get('rationale', ''))} |"
                  for item in structure["technology_choices"]]
        lines.append("")
    return "\n".join(lines)


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def render_architecture_json(structure: ArchitectureStructure) -> str:
    return json.dumps(structure, ensure_ascii=False, indent=2) + "\n"


@dataclass
class ArchitectureState:
    """Workflow input and query result; carried over by continue-as-new and forks."""

    source_session_id: str = ""
    kind: Literal["new_service", "api_change"] = "new_service"
    # The task's requirements, as the API design receives them: for an API change the
    # repository's contract with the change's requirements added.
    requirements: list[Requirement] = field(default_factory=list)
    requirements_document: str = ""
    # What an API change asks for; empty for a new service.
    change_document: str = ""
    # The repository's existing architecture an API change starts from, and keeps when
    # the stage is skipped.
    existing_architecture: str | None = None
    existing_structure: ArchitectureStructure | None = None
    messages: list[ConversationMessage] = field(default_factory=list)
    # The current draft; None until drafted.
    structure: ArchitectureStructure | None = None
    skip_proposal: SkipProposal | None = None
    skip_declined: bool = False
    status: ArchitectureStatus = "drafting"
    document: dict[str, Any] | None = None
    error: str | None = None
    forked_from: ForkOrigin | None = None
    repository: RepositorySelection | None = None
    checkout: RepositoryCheckout | None = None
    # The API design the session started once finalized or skipped.
    api_design_session_id: str | None = None
    # The start of the task's API design, without the architecture: what the session
    # hands on once finalized or skipped.
    design: ApiDesignState | None = None
    # The task flow the session is a stage of, and the API design it reserved for the
    # task; None for sessions started before task flows.
    task_flow_id: str | None = None
    next_session_id: str | None = None
    # Set for a stage created before the requirements were finalized: it waits for its
    # task flow to start it.
    pending: bool = False


@dataclass
class ArchitectureResult:
    """What a finalized or skipped architecture session hands to its requirements
    session: the API design to start, under this ID."""

    api_design_session_id: str
    design: ApiDesignState


def can_skip(state: ArchitectureState) -> bool:
    return state.kind in SKIPPABLE_KINDS
