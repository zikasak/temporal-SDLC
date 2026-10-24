"""The repository, branch and onboarding an execution works for, recorded on the execution
itself so that GET /workflows can show and filter by them.

Search attributes hold what the list filters on (the repository's full name in lower case,
compared case-insensitively as on GitHub, the source branch and the onboarding's session
ID); the memo holds the repository selection as the session has it. Safe to import in
Temporal workflows."""

from typing import Any

from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes

REPOSITORY_KEY = SearchAttributeKey.for_keyword("SdlcRepository")
BRANCH_KEY = SearchAttributeKey.for_keyword("SdlcBranch")
ONBOARDING_KEY = SearchAttributeKey.for_keyword("SdlcOnboardingId")
SEARCH_ATTRIBUTE_KEYS = (REPOSITORY_KEY, BRANCH_KEY, ONBOARDING_KEY)
REPOSITORY_MEMO = "repository"


def search_attributes(
    repository: dict[str, Any] | None, onboarding_id: str | None = None,
) -> TypedSearchAttributes:
    pairs: list[SearchAttributePair] = []
    if repository:
        pairs.append(SearchAttributePair(REPOSITORY_KEY, repository["full_name"].lower()))
        pairs.append(SearchAttributePair(BRANCH_KEY, repository["branch"]))
    if onboarding_id:
        pairs.append(SearchAttributePair(ONBOARDING_KEY, onboarding_id))
    return TypedSearchAttributes(pairs)


def memo(repository: dict[str, Any] | None) -> dict[str, Any]:
    return {REPOSITORY_MEMO: dict(repository)} if repository else {}


def labels(repository: dict[str, Any] | None, onboarding_id: str | None = None) -> dict[str, Any]:
    """Keyword arguments for starting an execution with its labels."""
    return {"search_attributes": search_attributes(repository, onboarding_id),
            "memo": memo(repository)}


def _literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def filter_query(
    workflow_type: str | None = None,
    repository: str | None = None,
    branch: str | None = None,
    onboarding_id: str | None = None,
) -> str | None:
    """A visibility query matching every given filter; None without any."""
    clauses = []
    if workflow_type:
        clauses.append(f"WorkflowType = {_literal(workflow_type)}")
    if repository is not None:
        clauses.append(f"{REPOSITORY_KEY.name} = {_literal(repository.lower())}")
    if branch is not None:
        clauses.append(f"{BRANCH_KEY.name} = {_literal(branch)}")
    if onboarding_id is not None:
        clauses.append(f"{ONBOARDING_KEY.name} = {_literal(onboarding_id)}")
    return " AND ".join(clauses) or None
