"""The repository, branch and onboarding an execution works for, as GET /workflows lists them.

Every OnboardingWorkflow, RequirementsWorkflow, ArchitectureWorkflow, ApiDesignWorkflow and
DeliveryWorkflow, and
every CodegenWorkflow a delivery starts, is started with them: as custom search attributes to
filter on (the repository lower-cased, as GitHub compares names case-insensitively) and the
whole repository selection in the memo. Continued runs keep both. Safe to import from
workflow code."""

from typing import Any

from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes

REPOSITORY_ATTRIBUTE = SearchAttributeKey.for_keyword("Repository")
BRANCH_ATTRIBUTE = SearchAttributeKey.for_keyword("Branch")
ONBOARDING_ATTRIBUTE = SearchAttributeKey.for_keyword("OnboardingId")
# A stage of a task flow that waits for the stages it depends on ("pending"), or that
# will never run ("skipped"); listings show it instead of the Temporal status.
FLOW_STATE_ATTRIBUTE = SearchAttributeKey.for_keyword("FlowState")
# The current short name of a task flow (TaskWorkflow).
TASK_NAME_ATTRIBUTE = SearchAttributeKey.for_keyword("TaskName")
# Whether the flow waits for the user, and whether its delivery has open review findings
# that were not accepted. Executions started before these existed carry neither, and are
# listed without them; a workflow started with them keeps them up to date itself.
WAITING_ATTRIBUTE = SearchAttributeKey.for_bool("WaitingForUser")
UNRESOLVED_ATTRIBUTE = SearchAttributeKey.for_bool("UnresolvedFindings")
SEARCH_ATTRIBUTES = (REPOSITORY_ATTRIBUTE, BRANCH_ATTRIBUTE, ONBOARDING_ATTRIBUTE,
                     FLOW_STATE_ATTRIBUTE, TASK_NAME_ATTRIBUTE, WAITING_ATTRIBUTE,
                     UNRESOLVED_ATTRIBUTE)
REPOSITORY_MEMO = "repository"
# The memo of a task flow: the workflow IDs of its own stages, of its delivery, and of the
# earlier stages of another task that a fork or fix round links it to.
STAGES_MEMO = "stages"
DELIVERY_MEMO = "delivery"
LINKED_MEMO = "linked"
PENDING = "pending"
SKIPPED = "skipped"


def execution_fields(repository: Any = None, onboarding_id: str | None = None, *,
                     flags: bool = False, findings: bool = False,
                     flow_state: str | None = None, name: str | None = None,
                     memo: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keyword arguments of start_workflow and of starting a child workflow. `flags`
    starts the execution with WaitingForUser (false), which has it keep the attribute up
    to date, and `findings` with UnresolvedFindings (false) too; `flow_state` marks a
    stage pending, `name` is a task flow's name and `memo` adds to the memo."""
    pairs: list[SearchAttributePair] = []
    memo = dict(memo or {})
    if repository:
        selection = dict(repository)
        memo[REPOSITORY_MEMO] = selection
        if selection.get("full_name"):
            pairs.append(SearchAttributePair(REPOSITORY_ATTRIBUTE,
                                             str(selection["full_name"]).lower()))
        if selection.get("branch"):
            pairs.append(SearchAttributePair(BRANCH_ATTRIBUTE, str(selection["branch"])))
    if onboarding_id:
        pairs.append(SearchAttributePair(ONBOARDING_ATTRIBUTE, onboarding_id))
    if flags:
        pairs.append(SearchAttributePair(WAITING_ATTRIBUTE, False))
    if findings:
        pairs.append(SearchAttributePair(UNRESOLVED_ATTRIBUTE, False))
    if flow_state:
        pairs.append(SearchAttributePair(FLOW_STATE_ATTRIBUTE, flow_state))
    if name:
        pairs.append(SearchAttributePair(TASK_NAME_ATTRIBUTE, name))
    fields: dict[str, Any] = {}
    if pairs:
        fields["search_attributes"] = TypedSearchAttributes(pairs)
    if memo:
        fields["memo"] = memo
    return fields


def task_fields(spec: Any, *, flags: bool = False) -> dict[str, Any]:
    """The fields of a delivery, and of the code generation it starts, from its TaskSpec:
    an onboarding's delivery is named by the onboarding's session ID. `flags` gives a
    delivery its WaitingForUser and UnresolvedFindings attributes."""
    onboarding_id = spec.session_id if getattr(spec, "onboarding", False) else None
    return execution_fields(getattr(spec, "repository", None), onboarding_id,
                            flags=flags, findings=flags)


def has_flags(attributes: TypedSearchAttributes | None) -> bool:
    """Whether an execution with these search attributes keeps WaitingForUser."""
    return bool(attributes) and attributes.get(WAITING_ATTRIBUTE) is not None
