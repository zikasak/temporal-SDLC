from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict

from app.conversations.models import ConversationMessage, ForkOrigin
from app.github.models import RepositoryCheckout, RepositorySelection

Category = Literal["functional", "non_functional", "technical"]
Priority = Literal["must", "should", "could"]
# A session is `preparing` while its repository is checked out, before any model turn.
SessionStatus = Literal["preparing", "interviewing", "turn_running", "finalizing", "completed",
                        "failed"]

CATEGORY_PREFIXES: dict[str, str] = {
    "functional": "FR",
    "non_functional": "NFR",
    "technical": "TR",
}
PRIORITIES = ("must", "should", "could")


class Requirement(TypedDict):
    id: str
    category: Category
    title: str
    description: str
    priority: Priority
    rationale: NotRequired[str]


@dataclass
class RequirementsState:
    """Workflow input and query result; carried over by continue-as-new."""

    messages: list[ConversationMessage] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    status: SessionStatus = "interviewing"
    document: dict[str, Any] | None = None
    error: str | None = None
    forked_from: ForkOrigin | None = None
    repository: RepositorySelection | None = None
    # Set once the repository is on the worker's disk; forks check out their own.
    checkout: RepositoryCheckout | None = None
