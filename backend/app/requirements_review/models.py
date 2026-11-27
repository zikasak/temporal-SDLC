"""What the requirements review holds and exchanges with the interview it reviews. Safe to
import from workflow code.

The review (RequirementsReviewWorkflow) is a child of the task flow, created pending when
the task flow starts. The requirements or onboarding interview hands it each round
(`review_round`), and the review signals the round's result back (`review_result`): the
contradictions it found, each asked of the user as a question, or why the round failed.
The interview keeps the review's state for its session responses (RequirementsReview)."""

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from app.github.models import RepositorySelection
from app.llm.questions import MAX_QUESTIONS, Question

# A requirement record as app.requirements.models.Requirement holds it; that module keeps
# the review's state in the session's, so it is not imported here.
Requirement = dict[str, Any]

REVIEW_FLOW_TYPE = "RequirementsReviewWorkflow"
ReviewStatus = Literal["not_started", "running", "passed", "contradictions_found", "failed",
                       "continued_anyway", "skipped"]
# The kinds of task whose requirements are reviewed; a documentation change is not.
REVIEWED_KINDS = ("new_service", "api_change", "bugfix")
# Every contradiction is asked as a question, and one message asks at most this many.
MAX_CONTRADICTIONS = MAX_QUESTIONS
CONTRADICTION_PREFIX = "C-"
# The review statuses after which the requirements document may be written.
CLEARED = ("passed", "continued_anyway")
# What the session responses report of the review.
PUBLIC_FIELDS = ("status", "round", "contradictions", "questions_open", "error")

ROUND_FAILED = "The requirements review could not be completed. Retry the review round."
UNREACHABLE = ("The requirements review could not be reached. Retry the review round, or "
               "continue anyway.")
ROUND_RUNNING = ("A requirements review round is running. Wait for it to finish, or continue "
                 "anyway.")
QUESTIONS_OPEN = ("Answer the contradiction questions of the requirements review first, or "
                  "continue anyway.")


class Contradiction(TypedDict):
    id: str
    summary: str
    requirement_ids: list[str]
    question: Question


def review_id(session_id: str) -> str:
    """The review of an interview session: requirements-session-<id> is reviewed by
    requirements-review-<id>, onboarding-session-<id> by onboarding-review-<id>."""
    return session_id.replace("-session-", "-review-", 1)


def reviewed_session_id(review_workflow_id: str) -> str:
    """The interview session `review_workflow_id` reviews."""
    return review_workflow_id.replace("-review-", "-session-", 1)


@dataclass
class ReviewRoundInput:
    """One round of the review: the requirements to check and what the reviewer is told
    of the earlier rounds."""

    session_id: str
    round: int
    requirements: list[Requirement] = field(default_factory=list)
    # The existing service's requirements the task has not retired.
    existing: list[Requirement] = field(default_factory=list)
    # From the second round on: what changed in the requirements since the previous one
    # (added, changed with before and after, removed).
    diff: dict[str, Any] | None = None
    # The contradictions of the previous round, each with the user's answer to its
    # question, or None when the question was dropped unanswered.
    previous: list[dict[str, Any]] = field(default_factory=list)
    # The number the next new contradiction is given (C-<number>).
    next_number: int = 1
    # Whether the existing requirements are those of the contract in the session's
    # checkout, read by the activity: an onboarding that replaces an existing contract.
    read_existing: bool = False
    # The kind of task the interview recorded as it was finalized: the task flow creates
    # the stages that kind needs with its first round. None for an onboarding.
    kind: str | None = None


@dataclass
class ReviewRoundResult:
    round: int
    contradictions: list[Contradiction] = field(default_factory=list)
    next_number: int = 1
    # Why the round failed for good; None when it finished.
    error: str | None = None


@dataclass
class RequirementsReview:
    """The review's state as the interview keeps it; carried over by continue-as-new and
    copied by a fork."""

    status: ReviewStatus = "not_started"
    round: int = 0
    contradictions: list[Contradiction] = field(default_factory=list)
    questions_open: bool = False
    error: str | None = None
    # Not reported. The requirements the last finished round reviewed, which the next
    # round's diff starts from.
    reviewed: list[Requirement] | None = None
    # The contradictions of the last finished round, kept when a later round failed: the
    # previous round of the next one, and what continuing anyway lists in the document.
    remaining: list[Contradiction] = field(default_factory=list)
    # The user's answers to the remaining contradictions' questions; None when unanswered.
    answers: list[str] | None = None
    next_number: int = 1
    # The input of the running or failed round, which a retry sends again.
    round_input: ReviewRoundInput | None = None
    # Set once the user answered the contradiction questions: the next round starts after
    # the interviewer's turn that applies the answers.
    after_turn: bool = False


def review_view(review: RequirementsReview | None) -> dict[str, Any]:
    """The review as the session responses report it."""
    review = review or RequirementsReview()
    return {name: getattr(review, name) for name in PUBLIC_FIELDS}


@dataclass
class ReviewFlowState:
    """Workflow input and query result of a RequirementsReviewWorkflow."""

    # The requirements session or onboarding the review checks.
    session_id: str = ""
    repository: RepositorySelection | None = None
    task_flow_id: str | None = None
    # Created by the task flow before the interview was finalized.
    pending: bool = False
    status: ReviewStatus = "not_started"
    round: int = 0
    # The contradictions of the last finished round; those that remained when the user
    # continued anyway.
    contradictions: list[Contradiction] = field(default_factory=list)
    error: str | None = None
    # Every round so far: its number, how many contradictions it found and any error.
    rounds: list[dict[str, Any]] = field(default_factory=list)
    # Rounds handed in and not yet run, carried over by continue-as-new.
    queued: list[ReviewRoundInput] = field(default_factory=list)


def requirements_diff(before: list[Requirement] | None,
                      after: list[Requirement]) -> dict[str, Any] | None:
    """What changed from `before` to `after`, by requirement ID; None without a before."""
    if before is None:
        return None
    earlier = {item["id"]: item for item in before}
    later = {item["id"]: item for item in after}
    return {
        "added": [item for item in after if item["id"] not in earlier],
        "changed": [{"before": earlier[item["id"]], "after": item} for item in after
                    if item["id"] in earlier and dict(earlier[item["id"]]) != dict(item)],
        "removed": [item for item in before if item["id"] not in later],
    }


def contradiction_message(contradictions: list[Contradiction]) -> dict[str, Any]:
    """The assistant message that asks the user one question per contradiction."""
    lines = ["The requirements review found "
             + ("a contradiction" if len(contradictions) == 1
                else f"{len(contradictions)} contradictions")
             + " between the requirements. Please say how to resolve "
             + ("it." if len(contradictions) == 1 else "each of them."), ""]
    for item in contradictions:
        ids = ", ".join(item["requirement_ids"])
        lines.append(f"- {item['id']} ({ids}): {item['summary']}")
    return {"role": "assistant", "content": "\n".join(lines),
            "questions": [item["question"] for item in contradictions]}


def previous_round(review: RequirementsReview) -> list[dict[str, Any]]:
    """The last finished round's contradictions with the user's answers, as the next
    round is told of them."""
    answers = review.answers or []
    return [{**item, "answer": answers[index] if index < len(answers) else None}
            for index, item in enumerate(review.remaining)]


def forked_review(review: RequirementsReview | None,
                  messages: list[Any]) -> RequirementsReview:
    """The review state a fork with `messages` (the conversation up to the fork point)
    starts from: the source's round and the contradictions of its last finished round,
    whose questions are still open when the fork ends with the message asking them. The
    fork's own review continues from that round."""
    if review is None:
        return RequirementsReview()
    remaining = list(review.remaining)
    questions = [item["question"] for item in remaining]

    def asks(message: Any) -> bool:
        return (isinstance(message, dict) and message.get("role") == "assistant"
                and bool(questions) and list(message.get("questions") or []) == questions)

    if not any(asks(message) for message in messages):
        # The fork point precedes the last round's contradictions.
        remaining = []
    asked = bool(messages) and asks(messages[-1])
    return RequirementsReview(
        status="contradictions_found" if remaining else "not_started",
        round=review.round,
        contradictions=remaining,
        questions_open=bool(remaining) and asked,
        reviewed=list(review.reviewed) if review.reviewed is not None else None,
        remaining=remaining,
        next_number=review.next_number,
    )
