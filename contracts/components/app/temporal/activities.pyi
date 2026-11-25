from typing import Any


async def open_pull_request(
    session_id: str, repository: Any, publication: Any,
    change: dict[str, Any], text: Any | None = None,
) -> Any:
    """Activity 'open_pull_request': open a pull request from the pushed branch to the
    repository's default branch, or update the open one of that branch; returns
    {'number', 'title', 'url', 'draft', 'base'}. Fails with an ApplicationError of type
    'PullRequestError', non-retryable, when it cannot, such as when the pull_requests
    permission is missing."""
    ...


async def publish_onboarding_architecture(
    session_id: str, repository: Any, architecture: str,
    structure: dict[str, Any] | None,
) -> Any:
    """Activity 'publish_onboarding_architecture': push the architecture an onboarding
    drafted to its own new branch; returns the publication {'branch', 'commit', 'url',
    'verified', 'ignored', 'ignored_count', 'findings', 'findings_count'}."""
    ...


async def pull_request_state(full_name: str, number: int) -> dict[str, Any]:
    """Activity 'pull_request_state': the state of pull request `number` of the
    repository `full_name` ('owner/name') on GitHub, as {'state': 'open' or 'closed',
    'merged': bool}. Run by a waiting delivery every PULL_REQUEST_CHECK_INTERVAL."""
    ...

async def _github_token() -> tuple[str, dict[str, Any]]:
    ...
