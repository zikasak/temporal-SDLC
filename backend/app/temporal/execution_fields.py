"""The repository, branch and onboarding an execution works for, as GET /workflows lists them.

Every TaskWorkflow, OnboardingWorkflow, RequirementsWorkflow, ArchitectureWorkflow,
ApiDesignWorkflow and DeliveryWorkflow, and
every CodegenWorkflow a delivery starts, is started with them: as custom search attributes to
filter on (the repository lower-cased, as GitHub compares names case-insensitively) and the
whole repository selection in the memo. Continued runs keep both. Safe to import from
workflow code."""

from typing import Any

from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes

REPOSITORY_ATTRIBUTE = SearchAttributeKey.for_keyword("Repository")
BRANCH_ATTRIBUTE = SearchAttributeKey.for_keyword("Branch")
ONBOARDING_ATTRIBUTE = SearchAttributeKey.for_keyword("OnboardingId")
SEARCH_ATTRIBUTES = (REPOSITORY_ATTRIBUTE, BRANCH_ATTRIBUTE, ONBOARDING_ATTRIBUTE)
REPOSITORY_MEMO = "repository"
# What GET /workflows lists beside Temporal's own fields, kept in the memo, which the
# workflows update as they go (app.temporal.flags): a stage of a task flow that waits for
# the stages before it ("pending") or will never run ("skipped"), a task flow's name, the
# earlier stages of another task a forked or fix-round task flow links to, the delivery
# child of a task flow, and whether the flow waits for the user or has unresolved review
# findings. A memo an execution does not have lists as null.
FLOW_STATUS_MEMO = "flow_status"
NAME_MEMO = "name"
LINKED_MEMO = "linked_stages"
DELIVERY_MEMO = "delivery_workflow_id"
WAITING_MEMO = "waiting_for_user"
FINDINGS_MEMO = "unresolved_findings"
PENDING = "pending"
SKIPPED = "skipped"


def execution_fields(repository: Any = None, onboarding_id: str | None = None,
                     memo_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keyword arguments of start_workflow and of starting a child workflow; `memo_fields`
    are added to the memo."""
    pairs: list[SearchAttributePair] = []
    memo: dict[str, Any] = dict(memo_fields or {})
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


def task_fields(spec: Any, memo_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """The fields of a delivery, and of the code generation it starts, from its TaskSpec:
    an onboarding's delivery is named by the onboarding's session ID."""
    onboarding_id = spec.session_id if getattr(spec, "onboarding", False) else None
    return execution_fields(getattr(spec, "repository", None), onboarding_id, memo_fields)


def pending_memo() -> dict[str, Any]:
    """The memo a stage of a task flow is created with while it waits for the stages
    before it."""
    return {FLOW_STATUS_MEMO: PENDING, WAITING_MEMO: False}
