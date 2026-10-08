"""Non-secret session metadata; safe to import in Temporal workflows."""

from typing import Literal, TypedDict


class RepositorySelection(TypedDict):
    provider: Literal["github"]
    repository_id: int
    full_name: str
    html_url: str
    private: bool
    branch: str
