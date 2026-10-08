"""Non-secret session metadata; safe to import in Temporal workflows."""

from typing import Literal, TypedDict


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
    """The new branch a session's verified sources were pushed to."""

    branch: str
    commit: str
    url: str


PublicationStatus = Literal['not_applicable', 'waiting', 'running', 'completed', 'skipped',
                            'error']
