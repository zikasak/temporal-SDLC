from typing import Literal
from typing import TypedDict
class RepositorySelection(TypedDict):
    provider: Literal['github']
    repository_id: int
    full_name: str
    html_url: str
    private: bool
    branch: str

from typing import Any
from typing import NotRequired
class RepositoryPublication(TypedDict):
    branch: str
    commit: str
    url: str
    verified: bool
    ignored: list[str]
    ignored_count: int
    findings: NotRequired[list[dict[str, Any]]]
    findings_count: NotRequired[int]
