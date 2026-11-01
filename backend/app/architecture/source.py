"""The architecture session a finished requirements session hands its task to. Safe to
import from workflow code."""

import dataclasses

from app.api_design.models import ApiDesignState
from app.api_design.source import design_state
from app.architecture.models import (
    ArchitectureState,
    normalize_structure,
    render_architecture_markdown,
)
from app.requirements.models import RequirementsState


def architecture_state(session_id: str, source: RequirementsState,
                       requirements_document: str) -> ArchitectureState:
    """The start of the architecture session of requirements session `session_id`."""
    design = design_state(session_id, source, requirements_document)
    contract = source.contract or {}
    return ArchitectureState(
        source_session_id=session_id,
        kind="api_change" if source.kind == "api_change" else "new_service",
        requirements=design.requirements,
        requirements_document=design.requirements_document,
        change_document=design.change_document,
        existing_architecture=contract.get("architecture"),
        existing_structure=contract.get("architecture_structure"),
        repository=source.repository,
        design=design,
    )


def handed_design(state: ArchitectureState, session_id: str) -> ApiDesignState:
    """The API design a finalized or skipped architecture session `session_id` starts."""
    design = dataclasses.replace(state.design or ApiDesignState(
        source_session_id=state.source_session_id, requirements=state.requirements,
        requirements_document=state.requirements_document, kind=state.kind,
        repository=state.repository))
    design.architecture_session_id = session_id
    if state.status == "skipped":
        # The repository's architecture stays as it is.
        design.architecture = state.existing_architecture
        design.architecture_structure = state.existing_structure
    else:
        structure = normalize_structure(state.structure)
        design.architecture = render_architecture_markdown(structure)
        design.architecture_structure = dict(structure)
    return design
