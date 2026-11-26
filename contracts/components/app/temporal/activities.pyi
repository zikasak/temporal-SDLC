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

from collections.abc import Callable
from app.delivery.contract import RepositoryContract
from app.github.models import RepositoryPublication
from app.github.models import RepositorySelection
from app.review.models import ReviewFinding
from app.delivery.workspace import SessionWorkspace
async def publish_repository_from(workspace: SessionWorkspace, session_id: str, repository: RepositorySelection, contract: RepositoryContract, kind: str | None=None, findings: list[ReviewFinding] | None=None, components: dict[str, Any] | None=None, resolved: str | None=None) -> RepositoryPublication:
    """Publish a delivery (kinds new service, 'api_change', 'bugfix') through _publish. The pushed commit's message is written by an LLM from that commit's diff only (not the task, requirements or pull request text): its subject names the specific change, not the fixed subject of the task kind (COMMIT_SUBJECT / COMMIT_SUBJECTS), and is at most 72 characters, imperative, followed by at most 5 lines of body, in the same format and from the same prompt as the wip/ commit messages of app.agent_workflows.host._work_message. The LLM runs through the native Claude adapter as _work_message's stub describes (a test fakes the saved workflow settings and app.llm.claude.ClaudeSDKClient). Besides it the message keeps: ' (unverified)' appended to the subject when `findings` are open, the list of open findings and FINDINGS_NOTE, the components note when `components` is given, and the line 'Generated from <branch> by session <id>.'. When the LLM cannot write (raises, times out, replies empty) the push still succeeds with the fixed subject of the task kind. The push is app.github.checkout.push, run as it is against a local remote; _github_token is faked. Helpers run_sync, session_language, published_files, is_service_file, is_documentation_file, onboarding_branch, commit_findings, read_routed_parts, component_suite, suite_members and write_contract run as they are."""
    ...
async def publish_onboarding_from(workspace: SessionWorkspace, session_id: str, repository: RepositorySelection, contract: RepositoryContract, findings: list[ReviewFinding] | None=None, code_changed: bool=False, resolved: str | None=None) -> RepositoryPublication:
    """Publish an onboarding through _publish to the onboarding branch. Its commit message is LLM-written from the commit's diff like publish_repository_from's, with the fixed ONBOARDING_SUBJECT only as the fallback when the LLM cannot write; ' (unverified)' marker, findings list, code-changed note and the 'Generated from <branch> by session <id>.' line are kept. Helpers run_sync, session_language, published_files, is_service_file, is_documentation_file, onboarding_branch, commit_findings, write_contract run as they are."""
    ...
async def publish_documentation_from(workspace: SessionWorkspace, session_id: str, repository: RepositorySelection, language: str, verified: bool, resolved: str | None=None) -> RepositoryPublication:
    """Publish documentation changes through _publish. Its commit message is LLM-written from the commit's diff like publish_repository_from's, with the fixed DOCUMENTATION_SUBJECT (with ' (unverified)' when not verified) as the fallback when the LLM cannot write; the 'Generated from <branch> by session <id>.' line and the unverified note are kept. Helpers run_sync, session_language, published_files, is_service_file, is_documentation_file, onboarding_branch, commit_findings, write_contract run as they are."""
    ...
async def _publish(workspace: SessionWorkspace, session_id: str, repository: RepositorySelection, subject: str, update: Callable[[], None], verified: bool=True, note: str=..., branch: str | None=None, details: str='', resolved: str | None=None) -> RepositoryPublication:
    """Apply `update`, commit to a branch and push with app.github.checkout.push. The message is the LLM-written text from the commit's diff (falling back to `subject` when the LLM cannot write, never failing the push), followed by the kept parts: ' (unverified)' marker when not `verified`, 'Generated from <branch> by session <id>.', `details`, and `note` when not verified. The subject the LLM wrote is at most 72 characters. Helpers run_sync, session_language, published_files, is_service_file, is_documentation_file, onboarding_branch, commit_findings, write_contract run as they are."""
    ...
COMMIT_SUBJECT: str
COMMIT_SUBJECTS: dict[str, str]
DOCUMENTATION_SUBJECT: str
ONBOARDING_SUBJECT: str
ARCHITECTURE_SUBJECT: str
UNVERIFIED_NOTE: str
FINDINGS_NOTE: str


async def pull_request_conflicts(full_name: str, number: int) -> dict[str, Any]:
    """Activity 'pull_request_conflicts' (task queue of the delivery, like the other
    activities of the delivery): how open pull request `number` of repository `full_name`
    ('owner/name') stands with its base branch on GitHub, as {'conflicting': bool,
    'base': base branch name, 'base_commit': tip commit of the base branch, 'files':
    [paths that conflict], 'changes': text of what the base branch changed in them}.
    A waiting delivery or onboarding (with a pull request and a repository) runs it, args
    [full_name, number], in its periodic check every PULL_REQUEST_CHECK_INTERVAL, right
    after 'pull_request_state' found the pull request open; a failed run changes nothing.
    It also runs when the signal 'pull_request_conflicted' arrives (see
    PullRequestWait.pull_request_conflicted). A result with conflicting true starts a base
    merge fix round (see publish_base_merge) unless one is already running or queued for
    that pull request, however many signals and checks report it."""
    ...


async def publish_base_merge(
    session_id: str, repository: Any, base_commit: str, files: list[str],
) -> Any:
    """Activity 'publish_base_merge': push the session's sources, as the base merge fix
    round left them, to the pull request branch as a merge commit that has `base_commit`
    (the base branch's tip) and the branch's own tip as parents; afterwards GitHub reports
    the pull request mergeable. Args [session_id, repository, base_commit, files]; returns
    the publication {'branch', 'commit', 'url', 'verified', 'ignored', 'ignored_count'}.
    Fails with an ApplicationError (non-retryable) when it cannot push.

    How a delivery or onboarding workflow uses it after 'pull_request_conflicts' reported
    conflicts (round number n counts with the push-conflict rounds towards
    MAX_MERGE_FIX_ROUNDS = 2 in a row, state.merge_fix = {'commit': base_commit, 'files',
    'round': n}):
    - It runs activity 'comment_pull_request' (args [full_name, number, text] -> comment
      URL) with a text naming the conflicting files and 'round n of 2', then the fix round
      (the delivery's usual agent and, for new service, API change and bugfix deliveries
      and onboardings producing the full contract, verification and code review), then
      this activity, then 'comment_pull_request' again with the outcome (the merge commit
      pushed, or what failed).
    - A documentation-only delivery pushes the merge right after its fix round, and an
      architecture-only onboarding runs no agent: it calls this activity right away; both
      without contract tests, verification or code review.
    - When round 2 has ended and the pull request still conflicts, no round starts; it
      comments once that the conflict remains and keeps waiting for the user.
    The round and its outcome show in the progress the 'get_state' query reports."""
    ...
