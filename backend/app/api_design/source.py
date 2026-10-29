"""The API design a finished requirements session hands its task to. Safe to import from
workflow code."""

from app.api_design.models import ApiDesignState
from app.delivery.contract import with_requirements
from app.requirements.models import RequirementsState


def design_state(session_id: str, source: RequirementsState,
                 requirements_document: str) -> ApiDesignState:
    """The start of the API design of requirements session `session_id`, whose document
    reads `requirements_document`."""
    state = ApiDesignState(
        source_session_id=session_id,
        requirements=source.requirements,
        requirements_document=requirements_document,
        kind=source.kind,
        repository=source.repository,
    )
    if source.contract:
        # A change to the service the repository holds: the design goes on from that
        # service's contract, which the change's requirements join.
        contract = with_requirements(source.contract, source.requirements, source.retired)
        state.requirements = contract["requirements"]
        state.requirements_document = contract["requirements_document"]
        state.change_document = requirements_document
        state.contract = source.contract
        state.contract_commit = source.contract_commit
        state.api = contract["api"]
        state.language = contract["language"]
    return state
