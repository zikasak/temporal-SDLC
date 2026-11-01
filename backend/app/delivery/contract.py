"""The contract a delivery publishes into its repository, where later tasks start from."""
import json
import shutil
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from app.agent_workflows.contracts import NARRATIVE_FILENAME, REQUIREMENTS_FILENAME
from app.api_design.models import ApiModel
from app.api_design.openapi import DOCUMENT_FILENAME as OPENAPI_FILENAME
from app.api_design.openapi import render_openapi
from app.architecture.models import (
    JSON_FILENAME as ARCHITECTURE_JSON_FILENAME,
    MARKDOWN_FILENAME as ARCHITECTURE_FILENAME,
    normalize_structure,
    render_architecture_json,
)
from app.github.checkout import CONTRACTS_DIR
from app.requirements.document import render_requirements_markdown, requirements_narrative
from app.requirements.models import CATEGORY_PREFIXES, Requirement

# The model the OpenAPI document is rendered from; the YAML alone cannot be read back.
API_FILENAME = 'api.json'
LANGUAGE_FILENAME = 'language.json'
UNSUPPORTED = 'Only repositories this pipeline published a service to are supported.'


class ContractError(RuntimeError):
    pass


class RepositoryContract(TypedDict):
    language: str
    requirements: list[Requirement]
    requirements_document: str
    api: ApiModel
    # contracts/architecture.md as it is, and contracts/architecture.json; a contract
    # from before architectures were written may lack them.
    architecture: NotRequired[str | None]
    architecture_structure: NotRequired[dict[str, Any] | None]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def with_requirements(contract: RepositoryContract, added: list[Requirement],
                      retired: list[Requirement]) -> RepositoryContract:
    """The contract after a change to its service: what the change required is now
    required of the service too, so later tasks keep the tests written for it, and what
    the change ended (`retired`) is not, nor is any endpoint linked to it any longer.
    `added` is numbered after the contract's own requirements already."""
    ended = {item['id'] for item in retired}
    requirements = [*(item for item in contract['requirements'] if item['id'] not in ended),
                    *added]
    api = {**contract['api'], 'endpoints': [
        {**endpoint, 'requirement_ids': [item for item in endpoint['requirement_ids']
                                         if item not in ended]}
        for endpoint in contract['api']['endpoints']]}
    # The document's tables render the list; one this pipeline did not render is kept.
    narrative = requirements_narrative(contract['requirements_document'],
                                       contract['requirements'])
    document = (contract['requirements_document'] if narrative is None
                else render_requirements_markdown(requirements, narrative))
    return {**contract, 'requirements': requirements, 'requirements_document': document,
            'api': api}


def write_architecture(repository: Path, architecture: str,
                       structure: dict[str, Any] | None) -> None:
    """Write contracts/architecture.md and contracts/architecture.json, replacing them;
    every other file of the contract stays as it is."""
    directory = repository / CONTRACTS_DIR
    directory.mkdir(exist_ok=True)
    (directory / ARCHITECTURE_FILENAME).write_text(
        architecture if architecture.endswith('\n') else architecture + '\n')
    if structure is not None:
        (directory / ARCHITECTURE_JSON_FILENAME).write_text(
            render_architecture_json(normalize_structure(structure)))


def write_contract(repository: Path, contract: RepositoryContract) -> None:
    """Make the repository's contract directory exactly this contract."""
    directory = repository / CONTRACTS_DIR
    # The architecture of a contract that carries none is kept as the repository has it.
    kept = {}
    if not contract.get('architecture'):
        for name in (ARCHITECTURE_FILENAME, ARCHITECTURE_JSON_FILENAME):
            if (directory / name).is_file():
                kept[name] = (directory / name).read_text()
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir()
    for name, text in {
        LANGUAGE_FILENAME: _json({'name': contract['language']}),
        REQUIREMENTS_FILENAME: _json(contract['requirements']),
        NARRATIVE_FILENAME: contract['requirements_document'],
        API_FILENAME: _json(contract['api']),
        OPENAPI_FILENAME: render_openapi(contract['api']),
        **kept,
    }.items():
        (directory / name).write_text(text)
    if contract.get('architecture'):
        write_architecture(repository, contract['architecture'],
                           contract.get('architecture_structure'))


def _unreadable(name: str, exc: Exception | None = None) -> ContractError:
    error = ContractError(
        f'The repository has no readable contract in {CONTRACTS_DIR}/: '
        f'{CONTRACTS_DIR}/{name} is missing or unreadable. {UNSUPPORTED}')
    if exc is not None:
        error.__cause__ = exc
    return error


def _read(directory: Path, name: str, parse: Any) -> Any:
    try:
        return parse((directory / name).read_text())
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise _unreadable(name, exc) from exc


def read_service_contract(repository: Path) -> RepositoryContract:
    """The contract of a repository this pipeline published, without its architecture;
    ContractError names the first file that is missing or unreadable."""
    directory = repository / CONTRACTS_DIR
    contract: RepositoryContract = {
        'language': _read(directory, LANGUAGE_FILENAME, lambda text: json.loads(text)['name']),
        'requirements': _read(directory, REQUIREMENTS_FILENAME, json.loads),
        'requirements_document': _read(directory, NARRATIVE_FILENAME, str),
        'api': _read(directory, API_FILENAME, json.loads),
    }
    # The files are a repository's content: anything a later stage would trip over is
    # found here, where it can still be reported as an unsupported repository.
    if not isinstance(contract['language'], str):
        raise _unreadable(LANGUAGE_FILENAME)
    try:
        if (not isinstance(contract['requirements'], list)
                or not all(isinstance(item['id'], str)
                           and item['category'] in CATEGORY_PREFIXES
                           for item in contract['requirements'])):
            raise TypeError('The requirements are not in the published form')
        render_requirements_markdown(contract['requirements'])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise _unreadable(REQUIREMENTS_FILENAME, exc) from exc
    try:
        render_openapi(contract['api'])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise _unreadable(API_FILENAME, exc) from exc
    return contract


def read_architecture(repository: Path) -> tuple[str, dict[str, Any]]:
    """contracts/architecture.md and the structure of contracts/architecture.json; both
    are required. ContractError names the one that is missing or unreadable."""
    directory = repository / CONTRACTS_DIR
    markdown = _read(directory, ARCHITECTURE_FILENAME, str)
    if not markdown.strip():
        raise _unreadable(ARCHITECTURE_FILENAME)
    structure = _read(directory, ARCHITECTURE_JSON_FILENAME,
                      lambda text: normalize_structure(json.loads(text)))
    return markdown, dict(structure)


def read_contract(repository: Path) -> RepositoryContract:
    """The contract of a repository this pipeline published, with its architecture; any
    other is unsupported."""
    contract = read_service_contract(repository)
    contract['architecture'], contract['architecture_structure'] = read_architecture(repository)
    return contract
