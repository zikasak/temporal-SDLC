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


class PullRequest(TypedDict):
    """The pull request opened from a delivery's pushed branch to the default branch."""

    number: int
    title: str
    url: str
    # True while the last push to its branch was unverified.
    draft: bool
    # The default branch it targets.
    base: str


class PullRequestMention(TypedDict):
    """An authorised mention of the app on a delivery's pull request and what it led to."""

    comment_id: int
    # A pull request conversation comment or an inline review comment.
    comment_type: Literal['conversation', 'review']
    url: str
    author: str
    # The comment's full text. A delivery's workflow carries its mentions without it;
    # the mention history keeps it, and the delivery progress shows it from there.
    body: NotRequired[str]
    received_at: str
    # pending until the LLM has classified the mention.
    classification: Literal['pending', 'fix', 'clarification', 'unclear']
    status: Literal['classifying', 'queued', 'running', 'replied', 'completed', 'error']
    # The first review round of the fix round the mention started.
    round: NotRequired[int]
    reply_url: NotRequired[str]
    error: NotRequired[str]
    # Where replies go, whatever pull request the delivery links to later: the repository
    # and pull request the comment was made on and, for an inline comment, the top-level
    # comment of its thread.
    repository: NotRequired[str]
    pull_number: NotRequired[int]
    reply_to: NotRequired[int]
    # Set by the delivery while the outcome of a stopped fix round is still to be replied
    # in the pull request; never shown.
    outcome_owed: NotRequired[bool]


PublicationStatus = Literal['not_applicable', 'waiting', 'running', 'completed', 'skipped',
                            'error']
