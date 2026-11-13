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
# A stage of a task flow that was created but waits for the stages it depends on
# (`pending`), or that will never run (`skipped`); listings show it instead of the
# Temporal status. Unset for every other execution.
FLOW_STATE_ATTRIBUTE = SearchAttributeKey.for_keyword("FlowState")
# The current short name of a task flow.
TASK_NAME_ATTRIBUTE = SearchAttributeKey.for_keyword("TaskName")
# Whether the flow waits for the user, and whether its delivery has open review findings
# that were not accepted; executions started before these existed carry neither.
WAITING_ATTRIBUTE = SearchAttributeKey.for_bool("WaitingForUser")
UNRESOLVED_ATTRIBUTE = SearchAttributeKey.for_bool("UnresolvedFindings")
SEARCH_ATTRIBUTES = (REPOSITORY_ATTRIBUTE, BRANCH_ATTRIBUTE, ONBOARDING_ATTRIBUTE,
                     FLOW_STATE_ATTRIBUTE, TASK_NAME_ATTRIBUTE, WAITING_ATTRIBUTE,
                     UNRESOLVED_ATTRIBUTE)
REPOSITORY_MEMO = "repository"
# The task flow a stage belongs to, kept with every stage it starts, also with a delivery
# started again on its own after its task flow closed.
TASK_FLOW_MEMO = "task_flow_id"
# A task flow's own record of its stages: their workflow IDs by stage and the earlier
# stages of another task it links to (a fork or a fix round).
TASK_STAGES_MEMO = "task_stages"
PENDING = "pending"
SKIPPED = "skipped"


def execution_fields(repository: Any = None, onboarding_id: str | None = None, *,
                     task_flow_id: str | None = None, pending: bool = False,
                     flags: bool = False, findings: bool = False) -> dict[str, Any]:
    """Keyword arguments of start_workflow and of starting a child workflow. A stage of
    a task flow carries the flow's ID in its memo and, while it waits for the stages it
    depends on, FlowState=pending. `flags` starts the execution as not waiting for the
    user, and `findings` as without unresolved review findings, so that listings tell it
    from executions started before either was kept."""
    pairs: list[SearchAttributePair] = []
    memo: dict[str, Any] = {}
    if task_flow_id:
        memo[TASK_FLOW_MEMO] = task_flow_id
    if pending:
        pairs.append(SearchAttributePair(FLOW_STATE_ATTRIBUTE, PENDING))
    if flags:
        pairs.append(SearchAttributePair(WAITING_ATTRIBUTE, False))
    if findings:
        pairs.append(SearchAttributePair(UNRESOLVED_ATTRIBUTE, False))
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


def task_fields(spec: Any, task_flow_id: str | None = None) -> dict[str, Any]:
    """The fields of a delivery, and of the code generation it starts, from its TaskSpec:
    an onboarding's delivery is named by the onboarding's session ID, and a task's
    delivery carries its task flow's ID."""
    onboarding_id = spec.session_id if getattr(spec, "onboarding", False) else None
    return execution_fields(getattr(spec, "repository", None), onboarding_id,
                            task_flow_id=task_flow_id)
