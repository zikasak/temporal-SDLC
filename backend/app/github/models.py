"""Non-secret session metadata; safe to import in Temporal workflows."""

from typing import Any, Literal, NotRequired, TypedDict


class RepositorySelection(TypedDict):
    provider: Literal["github"]
    repository_id: int
    full_name: str
    html_url: str
    private: bool
    branch: str


class RepositoryCheckout(TypedDict):
    """The commit of the selected branch a session works from."""

    commit: str


class RepositoryPublication(TypedDict):
    """The new branch a session's sources were pushed to."""

    branch: str
    commit: str
    url: str
    # False for sources pushed without a check of them, which their commit says too.
    verified: bool
    # Verified paths left out because the repository's .gitignore matches them (first few).
    ignored: list[str]
    ignored_count: int
    # The first open review findings, as the commit's message lists them, and the number
    # of all of them; empty and 0 for a commit pushed without open findings.
    findings: NotRequired[list[dict[str, Any]]]
    findings_count: NotRequired[int]


PublicationStatus = Literal['not_applicable', 'waiting', 'running', 'completed', 'skipped',
                            'error']
