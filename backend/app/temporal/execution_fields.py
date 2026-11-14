"""The repository, branch and onboarding an execution works for, as GET /workflows lists them.

Every OnboardingWorkflow, TaskWorkflow, RequirementsWorkflow, ArchitectureWorkflow,
ApiDesignWorkflow and DeliveryWorkflow, and
every CodegenWorkflow a delivery starts, is started with them: as custom search attributes to
filter on (the repository lower-cased, as GitHub compares names case-insensitively) and the
whole repository selection in the memo. Continued runs keep both. Safe to import from
workflow code.

The stages of a task flow also carry the task flow they belong to (TaskFlowId), even when a
delivery was started again on its own, and a stage created before it may work carries
FlowState pending, one that will never run FlowState skipped. A task flow carries its
short name (TaskName). Earlier stages a forked or fix-round task flow links to carry it in
LinkedTaskFlows. Stages, onboardings and task flows keep WaitingForUser and
UnresolvedFindings up to date as their state changes."""

from typing import Any

from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes

REPOSITORY_ATTRIBUTE = SearchAttributeKey.for_keyword("Repository")
BRANCH_ATTRIBUTE = SearchAttributeKey.for_keyword("Branch")
ONBOARDING_ATTRIBUTE = SearchAttributeKey.for_keyword("OnboardingId")
TASK_FLOW_ATTRIBUTE = SearchAttributeKey.for_keyword("TaskFlowId")
FLOW_STATE_ATTRIBUTE = SearchAttributeKey.for_keyword("FlowState")
TASK_NAME_ATTRIBUTE = SearchAttributeKey.for_keyword("TaskName")
LINKED_ATTRIBUTE = SearchAttributeKey.for_keyword_list("LinkedTaskFlows")
WAITING_ATTRIBUTE = SearchAttributeKey.for_bool("WaitingForUser")
UNRESOLVED_ATTRIBUTE = SearchAttributeKey.for_bool("UnresolvedFindings")
SEARCH_ATTRIBUTES = (REPOSITORY_ATTRIBUTE, BRANCH_ATTRIBUTE, ONBOARDING_ATTRIBUTE,
                     TASK_FLOW_ATTRIBUTE, FLOW_STATE_ATTRIBUTE, TASK_NAME_ATTRIBUTE,
                     LINKED_ATTRIBUTE, WAITING_ATTRIBUTE, UNRESOLVED_ATTRIBUTE)
REPOSITORY_MEMO = "repository"
# The earlier stages of another task a forked or fix-round task flow links to.
LINKED_MEMO = "linked"
# FlowState values: a stage created but not allowed to work yet, and one that never will.
PENDING = "pending"
SKIPPED = "skipped"
# The longest task name kept; keyword search attributes are bounded.
TASK_NAME_CHARS = 120


def task_name(text: str) -> str:
    """`text` as a task flow's name: one line, at most TASK_NAME_CHARS characters."""
    line = " ".join(str(text or "").split())
    if len(line) > TASK_NAME_CHARS:
        line = line[:TASK_NAME_CHARS - 1].rstrip() + "…"
    return line


def execution_fields(repository: Any = None, onboarding_id: str | None = None, *,
                     task_flow_id: str | None = None, pending: bool = False,
                     name: str | None = None,
                     linked: list[str] | None = None) -> dict[str, Any]:
    """Keyword arguments of start_workflow and of starting a child workflow."""
    pairs: list[SearchAttributePair] = []
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
    if task_flow_id:
        pairs.append(SearchAttributePair(TASK_FLOW_ATTRIBUTE, task_flow_id))
    if pending:
        pairs.append(SearchAttributePair(FLOW_STATE_ATTRIBUTE, PENDING))
    if name:
        pairs.append(SearchAttributePair(TASK_NAME_ATTRIBUTE, task_name(name)))
    if linked:
        memo[LINKED_MEMO] = list(linked)
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


def delivery_fields(spec: Any, *, pending: bool = False) -> dict[str, Any]:
    """The fields of a delivery itself: those of task_fields and, for a task's delivery,
    its task flow, which the code generation it starts does not carry."""
    onboarding_id = spec.session_id if getattr(spec, "onboarding", False) else None
    return execution_fields(getattr(spec, "repository", None), onboarding_id,
                            task_flow_id=getattr(spec, "task_flow_id", None), pending=pending)
