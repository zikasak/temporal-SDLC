from collections.abc import Iterable
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
KIND_PROBLEM = "The kind of task is not recorded. Tell the interviewer what kind it is."

# What the session's task is. Only the user can say which; it is never inferred.
TaskKind = Literal["new_service", "api_change", "bugfix", "docs"]
TASK_KINDS: dict[str, str] = {
    "new_service": "a new service built from nothing",
    "api_change": "a change to an existing service's API, such as a new endpoint or a "
                  "changed field",
    "bugfix": "a bugfix or another change to an existing service that leaves its API as "
              "it is",
    "docs": "a change to documentation only",
}
# Kinds that change the service a repository was published with: their requirements
# join its contract, and may end requirements of it.
CHANGE_KINDS = ("api_change", "bugfix")
# Kinds that work on a repository this pipeline published, and so start from the
# contract it holds.
CONTRACT_KINDS = (*CHANGE_KINDS, "docs")
# Those of them that leave the API as it is and are delivered straight from the interview.
DIRECT_KINDS = ("bugfix", "docs")


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
    # Unset until the user has said what kind of task this is.
    kind: TaskKind | None = None
    # Requirements of the repository's contract that a change to its service ends: they
    # leave the contract when the change is published.
    retired: list[Requirement] = field(default_factory=list)
    status: SessionStatus = "interviewing"
    document: dict[str, Any] | None = None
    error: str | None = None
    forked_from: ForkOrigin | None = None
    repository: RepositorySelection | None = None
    # Set once the repository is on the worker's disk; forks check out their own.
    checkout: RepositoryCheckout | None = None
    # The contract the repository was published with, read when a task on an existing
    # service is finalized. The delivery of a task that leaves the API as it is starts
    # from it here; the API design of any other starts from it.
    contract: dict[str, Any] | None = None
    # The commit of the checkout that contract was read from.
    contract_commit: str | None = None
    # The API design such a session started itself before the architecture stage existed;
    # no longer reported.
    api_design_session_id: str | None = None
    # The architecture session the session started itself once the user finalized a new
    # service or an API change; it hands the task on to the API design, which is a child
    # of this session too, as are forks of either.
    architecture_session_id: str | None = None


def numbered_after(requirements: list[Requirement], published: Iterable[str],
                   ) -> list[Requirement]:
    """The requirements of a change to a service, with a new ID for each one whose ID a
    requirement of the service's contract (`published`) already has. Applying it again
    changes nothing."""
    taken = set(published)
    result = []
    for item in requirements:
        if item["id"] in taken:
            prefix = CATEGORY_PREFIXES[item["category"]] + "-"
            numbers = [int(used[len(prefix):]) for used in taken
                       if used.startswith(prefix) and used[len(prefix):].isdigit()]
            item = {**item, "id": f"{prefix}{max(numbers, default=0) + 1}"}
        taken.add(item["id"])
        result.append(item)
    return result
