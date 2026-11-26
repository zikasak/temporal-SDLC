from pathlib import Path
from dataclasses import dataclass
@dataclass(frozen=True)
class SessionWorkspace:
    root: Path

    @property
    def checkout(self) -> Path:
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
