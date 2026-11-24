"""Where a session's files are on the worker's disk.

Each session has one directory, `<GENERATED_CODE_DIR>/<session_id>/`:

    repository/              the Git working copy of the session's repository
    implementation/service/  the service its author wrote (and its unit tests)
    tests/suite/             the contract tests their author wrote
    component-tests/suite/   the component tests, per part of the repository
    documentation/files/     the documentation its author changed

The stages write these; publication reads them into `repository/` and pushes it.
"""

from dataclasses import dataclass
from pathlib import Path

from app.github.checkout import CHECKOUT_DIR, checkout_path
from app.verification.models import COMPONENT_TESTS_ARTIFACT


@dataclass(frozen=True)
class SessionWorkspace:
    root: Path

    @classmethod
    def of(cls, generated_code_dir: Path, session_id: str) -> 'SessionWorkspace':
        """The workspace of `session_id`; an invalid ID raises CheckoutError."""
        return cls(checkout_path(generated_code_dir, session_id).parent)

    @property
    def checkout(self) -> Path:
        return self.root / CHECKOUT_DIR

    @property
    def service(self) -> Path:
        return self.root / 'implementation' / 'service'

    @property
    def suite(self) -> Path:
        return self.root / 'tests' / 'suite'

    @property
    def component_tests(self) -> Path:
        return self.root / COMPONENT_TESTS_ARTIFACT / 'suite'

    @property
    def documentation(self) -> Path:
        return self.root / 'documentation' / 'files'
