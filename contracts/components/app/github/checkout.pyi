from collections.abc import Callable
from pathlib import Path
from app.github.models import RepositoryPublication
from app.github.models import RepositorySelection
async def push(target: Path, repository: RepositorySelection, token: str, *, branch: str, message: str, author: tuple[str, str], verified: bool=True, resolved: str | None=None) -> RepositoryPublication:
    """Commit the working copy of `target` to `branch` with `message` and push it to origin (git run for real; a test points origin at a local bare repository and gives any token). The commit's diff is what the working copy changed. Runs as it is; validate_selection and run_sync run as they are."""
    ...
def publication_branch(session_id: str) -> str:
    """The branch a session's publication is pushed to. Runs as it is."""
    ...
def replace_files(source: Path, target: Path, include: Callable[[Path], bool]) -> None:
    """Copy the files of `source` that `include` accepts into `target`. Runs as it is; copy_file runs as it is."""
    ...
