from typing import Literal
from typing import NotRequired
from typing import TypedDict
class ReviewFinding(TypedDict):
    summary: str
    path: NotRequired[str]
    target: ReviewTarget
    reason: str
    repeats: NotRequired[int]
    unresolved: NotRequired[str]
ReviewTarget = Literal['implementation', 'tests', 'both']

def commit_findings(findings: list[ReviewFinding]) -> str:
    ...
