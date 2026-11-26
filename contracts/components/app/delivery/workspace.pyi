from pathlib import Path


class SessionWorkspace:
    """Where a session's files live on the worker's disk under `<GENERATED_CODE_DIR>/<session_id>/`.

    `checkout` points to `<root>/repository` (the Git working copy).
    """

    root: Path

    def __init__(self, root: Path) -> None:
        ...

    @classmethod
    def of(cls, generated_code_dir: Path, session_id: str) -> 'SessionWorkspace':
        """The workspace of `session_id` (`generated_code_dir / session_id`)."""
        ...

    @property
    def checkout(self) -> Path:
        """The Git working copy (`root / 'repository'`)."""
        ...

    @property
    def service(self) -> Path:
        ...

    @property
    def suite(self) -> Path:
        ...

    @property
    def component_tests(self) -> Path:
        ...

    @property
    def documentation(self) -> Path:
        ...
