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
# A stage of a task flow that was created but waits for the stages before it (pending), or
# one that will never run (skipped); listings show it instead of the Temporal status.
FLOW_STATE_ATTRIBUTE = SearchAttributeKey.for_keyword("FlowState")
# A task flow's current short name.
TASK_NAME_ATTRIBUTE = SearchAttributeKey.for_keyword("TaskName")
# Whether the flow waits for the user, and whether its delivery has open review findings
# that were not accepted; upserted by stages, onboardings and task flows as they change.
WAITING_ATTRIBUTE = SearchAttributeKey.for_bool("WaitingForUser")
UNRESOLVED_ATTRIBUTE = SearchAttributeKey.for_bool("UnresolvedFindings")
SEARCH_ATTRIBUTES = (REPOSITORY_ATTRIBUTE, BRANCH_ATTRIBUTE, ONBOARDING_ATTRIBUTE,
                     FLOW_STATE_ATTRIBUTE, TASK_NAME_ATTRIBUTE, WAITING_ATTRIBUTE,
                     UNRESOLVED_ATTRIBUTE)
REPOSITORY_MEMO = "repository"
# A task flow's stages: {"stages": [workflow IDs of its own stages], "linked": [earlier
# stages of another task it links to], "delivery": its delivery's workflow ID}.
TASK_FLOW_MEMO = "task_flow"
PENDING = "pending"
SKIPPED = "skipped"


def execution_fields(repository: Any = None, onboarding_id: str | None = None,
                     flow_state: str | None = None) -> dict[str, Any]:
    """Keyword arguments of start_workflow and of starting a child workflow; `flow_state`
    starts a stage of a task flow as pending."""
    pairs: list[SearchAttributePair] = []
    if flow_state:
        pairs.append(SearchAttributePair(FLOW_STATE_ATTRIBUTE, flow_state))
    memo: dict[str, Any] = {}
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
    fields: dict[str, Any] = {}
    if pairs:
        fields["search_attributes"] = TypedSearchAttributes(pairs)
    if memo:
        fields["memo"] = memo
    return fields


def task_fields(spec: Any) -> dict[str, Any]:
    """The fields of a delivery, and of the code generation it starts, from its TaskSpec:
    an onboarding's delivery is named by the onboarding's session ID."""
    onboarding_id = spec.session_id if getattr(spec, "onboarding", False) else None
    return execution_fields(getattr(spec, "repository", None), onboarding_id)
