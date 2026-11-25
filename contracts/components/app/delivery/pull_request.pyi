from typing import TypedDict
class PullRequestText(TypedDict):
    title: str
    summary: str

def parse_text(reply: str) -> PullRequestText:
    ...
