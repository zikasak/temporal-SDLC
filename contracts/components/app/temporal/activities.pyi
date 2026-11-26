from typing import Any

from app.delivery.workspace import SessionWorkspace


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


def _workspace(session_id: str) -> SessionWorkspace:
    """The session's workspace on this worker (`SessionWorkspace.of(GENERATED_CODE_DIR, session_id)`)."""
    ...


async def publish_onboarding_architecture(
    session_id: str, repository: Any, architecture: str,
    structure: dict[str, Any] | None,
) -> Any:
    """Activity 'publish_onboarding_architecture': delegates to
    `publish_onboarding_architecture_from(_workspace(session_id), ...)` to push the
    architecture an onboarding drafted to its own new branch. Expects an existing git
    checkout at `GENERATED_CODE_DIR / session_id / 'repository'` (it does not clone
    origin), calls `_github_token()`, and pushes via `repository_checkout.push`; returns
    the publication {'branch', 'commit', 'url', 'verified', 'ignored', 'ignored_count',
    'findings', 'findings_count'}."""
    ...


async def publish_onboarding_architecture_from(
    workspace: SessionWorkspace, session_id: str, repository: Any,
    architecture: str, structure: dict[str, Any] | None,
) -> Any:
    """Commit `contracts/architecture.md` and `contracts/architecture.json` into
    `workspace.checkout` (an existing git working copy; does not clone origin) on a new
    branch and push it using `_github_token()`."""
    ...


async def pull_request_state(full_name: str, number: int) -> dict[str, Any]:
    """Activity 'pull_request_state': the state of pull request `number` of the
    repository `full_name` ('owner/name') on GitHub, as {'state': 'open' or 'closed',
    'merged': bool}. Run by a waiting delivery every PULL_REQUEST_CHECK_INTERVAL."""
    ...

async def _github_token() -> tuple[str, dict[str, Any]]:
    ...
