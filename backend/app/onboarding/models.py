"""What an onboarding holds: the drafts of the contract and how far the review has come."""
from dataclasses import dataclass, field
from typing import Any, Literal

from app.api_design.models import ApiModel, empty_api
from app.api_design.openapi import design_problems
from app.architecture.models import architecture_problems
from app.conversations.models import ConversationMessage, ForkOrigin
from app.github.models import RepositoryCheckout, RepositorySelection
from app.requirements.models import Requirement
from app.requirements_review.models import RequirementsReview

# `preparing` while the branch is checked out (again); `awaiting_confirmation` while the
# user decides about the contract, contract tests or Dockerfile the branch already has;
# `drafting` while the LLM drafts the contract from the code; `superseded` once forked.
OnboardingStatus = Literal["preparing", "awaiting_confirmation", "declined", "drafting",
                           "interviewing", "turn_running", "finalizing", "completed",
                           "failed", "superseded"]
# What the onboarding replaces when the branch already has it.
Artifact = Literal["contract", "contract_tests", "dockerfile"]
ARTIFACTS = ("contract", "contract_tests", "dockerfile")
# The user's choice about what the branch already has.
Decision = Literal["replace", "decline", "architecture_only"]
DECISIONS = ("replace", "decline", "architecture_only")
# An onboarding in one of these is over: it takes no messages and cannot be forked.
FINISHED_STATUSES = ("completed", "failed", "declined", "superseded")
NO_REQUIREMENTS = "No requirements have been recorded yet."
NOT_DRAFTED = "The contract has not been drafted yet."
ARCHITECTURE_PRESENT = ("The branch's contract already has contracts/architecture.md and "
                        "contracts/architecture.json, so there is no architecture to add "
                        "alone.")
CONFLICT_PREFIX = "The architecture disagrees with the existing contract: "
ARTIFACTS_UNKNOWN = ("The existing contract, contract tests and Dockerfile of the checked-out "
                     "commit could not be read. Update the checkout again.")


def onboarding_branch(source_branch: str, session_id: str) -> str:
    """The branch an onboarding's result is pushed to."""
    return f"onboarding/{source_branch}-{session_id.rpartition('-session-')[2][:12]}"


@dataclass
class OnboardingState:
    """Workflow input and query result; carried over by continue-as-new and forks."""

    repository: RepositorySelection | None = None
    messages: list[ConversationMessage] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    api: ApiModel = field(default_factory=empty_api)
    # The implementation language of the existing code, as drafted and corrected.
    language: str | None = None
    status: OnboardingStatus = "preparing"
    error: str | None = None
    forked_from: ForkOrigin | None = None
    # The commit of the branch the onboarding reads; changes when the checkout is updated.
    checkout: RepositoryCheckout | None = None
    # What the branch had at the start or the last checkout update.
    existing_artifacts: list[Artifact] = field(default_factory=list)
    # Whether the user chose to replace what existing_artifacts lists.
    replaced_contract: bool = False
    # Whether the drafts were written; until then the interview cannot start.
    drafted: bool = False
    # Whether the last checkout update could not read what the new commit has, so new
    # artifacts may exist that the user was not asked about; finalizing waits until known.
    artifacts_unknown: bool = False
    # Whether the branch's contract lacks contracts/architecture.md or architecture.json,
    # which offers adding only those; and why the rest of that contract cannot be kept,
    # when it is unreadable.
    architecture_missing: bool = False
    contract_problem: str | None = None
    # Whether the user chose to add only the architecture to the existing contract.
    architecture_only: bool = False
    # Which kind of draft exists: True for an architecture-only draft, False for the full
    # contract. Unlike architecture_only, a new decision does not reset it, so a switch
    # between the two kinds drafts anew. None in histories from before it was recorded.
    drafted_architecture_only: bool | None = None
    # The architecture drafted from the code, as corrected in the interview.
    architecture_structure: dict[str, Any] | None = None
    # What an architecture-only draft disagrees with in the existing api.json or
    # requirements.json, as the LLM recorded it; each blocks finalizing.
    conflicts: list[str] = field(default_factory=list)
    # The top-level task flow (TaskWorkflow with origin onboarding) the onboarding interview
    # is a stage of, which starts its delivery; None for onboardings started before, which
    # deliver themselves.
    task_flow_id: str | None = None
    # The task flow's requirements review of the drafted requirements, and its state.
    review_id: str | None = None
    review: RequirementsReview = field(default_factory=RequirementsReview)


def drafted_architecture_only(state: OnboardingState) -> bool:
    """Whether the existing drafts are architecture-only ones."""
    if state.drafted_architecture_only is None:
        return state.architecture_only
    return state.drafted_architecture_only


def draft_kind_changed(state: OnboardingState, architecture_only: bool) -> bool:
    """Whether drafts exist of another kind than `architecture_only` asks for, so they do
    not carry over and the onboarding drafts anew."""
    return state.drafted and drafted_architecture_only(state) != architecture_only


def onboarding_problems(state: OnboardingState) -> list[str]:
    """What must be settled before the onboarding can be finalized."""
    if not state.drafted:
        return [NOT_DRAFTED]
    unknown = [ARTIFACTS_UNKNOWN] if state.artifacts_unknown else []
    architecture = architecture_problems(state.architecture_structure)  # type: ignore[arg-type]
    if state.architecture_only:
        return [*unknown, *architecture,
                *(f"{CONFLICT_PREFIX}{item}" for item in state.conflicts)]
    return [*unknown,
            *([] if state.requirements else [NO_REQUIREMENTS]),
            *design_problems(state.api, state.language),
            *architecture]
