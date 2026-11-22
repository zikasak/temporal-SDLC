"""The repository and branch an execution works for, as GET /workflows lists them.

Every TaskWorkflow, OnboardingWorkflow, RequirementsWorkflow, RequirementsReviewWorkflow,
ArchitectureWorkflow, ApiDesignWorkflow and DeliveryWorkflow, and
every CodegenWorkflow a delivery starts, is started with them: as custom search attributes to
filter on (the repository lower-cased, as GitHub compares names case-insensitively) and the
whole repository selection in the memo. Continued runs keep both. Safe to import from
workflow code."""

from typing import Any

from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes

REPOSITORY_ATTRIBUTE = SearchAttributeKey.for_keyword("Repository")
BRANCH_ATTRIBUTE = SearchAttributeKey.for_keyword("Branch")
# The onboarding an execution worked for; set only by executions started before
# onboardings ran under task flows, and no longer listed.
ONBOARDING_ATTRIBUTE = SearchAttributeKey.for_keyword("OnboardingId")
# What started a task flow: a requirements interview or an onboarding (or a fork or fix
# round of one); the origin filter of GET /workflows queries it.
TASK_ORIGIN_ATTRIBUTE = SearchAttributeKey.for_keyword("TaskOrigin")
# A stage of a task flow that waits for the stages it depends on ('pending'), that will
# never run ('skipped'), or that handed the task on and stays open only until its task flow
# closes ('completed'); unset while it works. Listings show it instead of the Temporal
# status.
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
                     WAITING_ATTRIBUTE, UNRESOLVED_ATTRIBUTE, TASK_ORIGIN_ATTRIBUTE)
REPOSITORY_MEMO = "repository"
# The memo of a task flow: the earlier stages of another task it links to, and the session
# its delivery is named after.
LINKED_STAGES_MEMO = "linked_stages"
DELIVERY_SESSION_MEMO = "delivery_session_id"
PENDING = "pending"
SKIPPED = "skipped"
HANDED_OFF = "completed"


def execution_fields(repository: Any = None, onboarding_id: str | None = None, *,
                     pending: bool = False, task_name: str | None = None,
                     task_origin: str | None = None,
                     linked: list[str] | None = None,
                     memo: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keyword arguments of start_workflow and of starting a child workflow: `pending`
    for a stage that waits for its task flow's start signal, `task_name` and `task_origin`
    for a task flow, `linked` for the task flows the execution is listed under besides its
    parent. `onboarding_id` is kept for executions recorded with it."""
    pairs: list[SearchAttributePair] = []
    memo = dict(memo or {})
    if pending:
        pairs.append(SearchAttributePair(FLOW_STATE_ATTRIBUTE, PENDING))
    if task_name:
        pairs.append(SearchAttributePair(TASK_NAME_ATTRIBUTE, task_name))
    if task_origin:
        pairs.append(SearchAttributePair(TASK_ORIGIN_ATTRIBUTE, task_origin))
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
    """The fields of a delivery, and of the code generation it starts, from its TaskSpec.
    An onboarding's delivery is reached through its task flow, not an onboarding ID."""
    return execution_fields(getattr(spec, "repository", None))
