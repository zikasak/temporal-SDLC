from typing import Any
from app.api_design.models import ApiModel
from typing import NotRequired
from app.requirements.models import Requirement
from typing import TypedDict
class RepositoryContract(TypedDict):
    language: str
    requirements: list[Requirement]
    requirements_document: str
    api: ApiModel
    architecture: NotRequired[str | None]
    architecture_structure: NotRequired[dict[str, Any] | None]

from pathlib import Path
def write_contract(repository: Path, contract: RepositoryContract) -> None:
    ...
