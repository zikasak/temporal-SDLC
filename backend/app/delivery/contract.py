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
from app.requirements.document import render_requirements_markdown, requirements_narrative
from app.requirements.models import CATEGORY_PREFIXES, Requirement

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


def with_requirements(contract: RepositoryContract,
                      added: list[Requirement]) -> RepositoryContract:
    """The contract after a change that leaves the API as it is: what the change required
    is now required of the service too, so later tasks keep the tests written for it.
    `added` is numbered after the contract's own requirements already."""
    requirements = [*contract['requirements'], *added]
    # The document's tables render the list; one this pipeline did not render is kept.
    narrative = requirements_narrative(contract['requirements_document'],
                                       contract['requirements'])
    document = (contract['requirements_document'] if narrative is None
                else render_requirements_markdown(requirements, narrative))
    return {**contract, 'requirements': requirements, 'requirements_document': document}


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
        contract: RepositoryContract = {
            'language': json.loads((directory / LANGUAGE_FILENAME).read_text())['name'],
            'requirements': json.loads((directory / REQUIREMENTS_FILENAME).read_text()),
            'requirements_document': (directory / NARRATIVE_FILENAME).read_text(),
            'api': json.loads((directory / API_FILENAME).read_text()),
        }
        # The files are a repository's content: anything a later stage would trip over
        # is found here, where it can still be reported as an unsupported repository.
        if (not isinstance(contract['language'], str)
                or not isinstance(contract['requirements'], list)
                or not all(isinstance(item['id'], str)
                           and item['category'] in CATEGORY_PREFIXES
                           for item in contract['requirements'])):
            raise TypeError('The contract is not in the published form')
        render_requirements_markdown(contract['requirements'])
        render_openapi(contract['api'])
        return contract
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ContractError(
            f'The repository has no readable contract in {CONTRACTS_DIR}/. Only repositories '
            'this pipeline published a service to are supported.') from exc
