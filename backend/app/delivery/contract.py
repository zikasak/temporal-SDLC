"""The contract a delivery publishes into its repository, where later tasks start from."""
import json
import shutil
from pathlib import Path
from typing import TypedDict

from app.agent_workflows.contracts import NARRATIVE_FILENAME, REQUIREMENTS_FILENAME
from app.api_design.models import ApiModel
from app.api_design.openapi import DOCUMENT_FILENAME as OPENAPI_FILENAME
from app.api_design.openapi import render_openapi
from app.github.checkout import CONTRACTS_DIR
from app.requirements.models import Requirement

# The model the OpenAPI document is rendered from; the YAML alone cannot be read back.
API_FILENAME = 'api.json'
LANGUAGE_FILENAME = 'language.json'


class ContractError(RuntimeError):
    pass


class RepositoryContract(TypedDict):
    language: str
    requirements: list[Requirement]
    requirements_document: str
    api: ApiModel


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def write_contract(repository: Path, contract: RepositoryContract) -> None:
    """Make the repository's contract directory exactly this contract."""
    directory = repository / CONTRACTS_DIR
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir()
    for name, text in {
        LANGUAGE_FILENAME: _json({'name': contract['language']}),
        REQUIREMENTS_FILENAME: _json(contract['requirements']),
        NARRATIVE_FILENAME: contract['requirements_document'],
        API_FILENAME: _json(contract['api']),
        OPENAPI_FILENAME: render_openapi(contract['api']),
    }.items():
        (directory / name).write_text(text)


def read_contract(repository: Path) -> RepositoryContract:
    """The contract of a repository this pipeline published; any other is unsupported."""
    directory = repository / CONTRACTS_DIR
    try:
        return {
            'language': json.loads((directory / LANGUAGE_FILENAME).read_text())['name'],
            'requirements': json.loads((directory / REQUIREMENTS_FILENAME).read_text()),
            'requirements_document': (directory / NARRATIVE_FILENAME).read_text(),
            'api': json.loads((directory / API_FILENAME).read_text()),
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ContractError(
            f'The repository has no readable contract in {CONTRACTS_DIR}/. Only repositories '
            'this pipeline published a service to are supported.') from exc
