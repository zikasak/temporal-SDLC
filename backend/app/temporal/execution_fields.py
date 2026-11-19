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
# A stage of a task flow that waits for the stages it depends on ('pending'), or that will
# never run ('skipped'); unset while it works or once it closed otherwise. Listings show
# it instead of the Temporal status.
FLOW_STATE_ATTRIBUTE = SearchAttributeKey.for_keyword("FlowState")
# A task flow's current short name.
TASK_NAME_ATTRIBUTE = SearchAttributeKey.for_keyword("TaskName")
# The task flows a flow is listed under besides its parent, such as the task flow of a
# delivery started again on its own after that task flow closed.
LINKED_ATTRIBUTE = SearchAttributeKey.for_keyword_list("LinkedTaskFlows")
# Whether the flow waits for the user, and whether its delivery has open review findings
# nobody accepted; unset for flows that cannot wait or have no delivery.
WAITING_ATTRIBUTE = SearchAttributeKey.for_bool("WaitingForUser")
UNRESOLVED_ATTRIBUTE = SearchAttributeKey.for_bool("UnresolvedFindings")
SEARCH_ATTRIBUTES = (REPOSITORY_ATTRIBUTE, BRANCH_ATTRIBUTE, ONBOARDING_ATTRIBUTE,
                     FLOW_STATE_ATTRIBUTE, TASK_NAME_ATTRIBUTE, LINKED_ATTRIBUTE,
                     WAITING_ATTRIBUTE, UNRESOLVED_ATTRIBUTE)
REPOSITORY_MEMO = "repository"
# The memo of a task flow: the earlier stages of another task it links to, and the session
# its delivery is named after.
LINKED_STAGES_MEMO = "linked_stages"
DELIVERY_SESSION_MEMO = "delivery_session_id"
PENDING = "pending"
SKIPPED = "skipped"


def execution_fields(repository: Any = None, onboarding_id: str | None = None, *,
                     pending: bool = False, task_name: str | None = None,
                     linked: list[str] | None = None,
                     memo: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keyword arguments of start_workflow and of starting a child workflow: `pending`
    for a stage that waits for its task flow's start signal, `task_name` for a task flow,
    `linked` for the task flows the execution is listed under besides its parent."""
    pairs: list[SearchAttributePair] = []
    memo = dict(memo or {})
    if pending:
        pairs.append(SearchAttributePair(FLOW_STATE_ATTRIBUTE, PENDING))
    if task_name:
        pairs.append(SearchAttributePair(TASK_NAME_ATTRIBUTE, task_name))
    if linked:
        pairs.append(SearchAttributePair(LINKED_ATTRIBUTE, list(linked)))
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
