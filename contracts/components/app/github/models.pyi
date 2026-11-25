from typing import Literal
from typing import TypedDict
class RepositorySelection(TypedDict):
    provider: Literal['github']
    repository_id: int
    full_name: str
    html_url: str
    private: bool
    branch: str
