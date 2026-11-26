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
from app.github.checkout import COMPONENTS_DIR, CONTRACTS_DIR
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
    api: ApiModel = {**contract['api'], 'endpoints': [
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
    """Make the repository's contract directory exactly this contract. Its component
    contract (`components/`) is not part of it and stays as the repository has it: a
    delivery with component tests replaces it on its own."""
    directory = repository / CONTRACTS_DIR
    # The architecture of a contract that carries none is kept as the repository has it.
    kept = {}
    if not contract.get('architecture'):
        for name in (ARCHITECTURE_FILENAME, ARCHITECTURE_JSON_FILENAME):
            if (directory / name).is_file():
                kept[name] = (directory / name).read_text()
    if directory.is_dir() and not directory.is_symlink():
        for item in directory.iterdir():
            if item.is_symlink() or not item.is_dir():
                item.unlink()
            elif item.name != COMPONENTS_DIR:
                shutil.rmtree(item)
    elif directory.exists() or directory.is_symlink():
        directory.unlink()
    directory.mkdir(exist_ok=True)
    for name, text in {
        LANGUAGE_FILENAME: _json({'name': contract['language']}),
        REQUIREMENTS_FILENAME: _json(contract['requirements']),
        NARRATIVE_FILENAME: contract['requirements_document'],
        API_FILENAME: _json(contract['api']),
        OPENAPI_FILENAME: render_openapi(contract['api']),
        **kept,
    }.items():
        (directory / name).write_text(text)
    architecture = contract.get('architecture')
    if architecture:
        write_architecture(repository, architecture,
                           contract.get('architecture_structure'))


# A value one side of a merge does not have.
_MISSING: Any = object()
# The fields that tell the items of a contract list apart, most specific first:
# requirements by ID, endpoints by method and path, schemas, properties and parameters by
# name.
_IDENTITIES = (('id',), ('method', 'path'), ('name',))
_JSON_FILES = (LANGUAGE_FILENAME, REQUIREMENTS_FILENAME, API_FILENAME,
               ARCHITECTURE_JSON_FILENAME)


def _identity(*lists: list[Any]) -> tuple[str, ...] | None:
    """The fields every item of `lists` has and that tell the items of each apart."""
    items = [item for values in lists for item in values]
    if not items or not all(isinstance(item, dict) for item in items):
        return None
    for fields in _IDENTITIES:
        if not all(all(field in item for field in fields) for item in items):
            continue
        if all(len({_key(item, fields) for item in values}) == len(values) for values in lists):
            return fields
    return None


def _key(item: dict[str, Any], fields: tuple[str, ...]) -> str:
    return json.dumps([item[field] for field in fields], sort_keys=True, default=str)


def _merge_value(base: Any, ours: Any, theirs: Any) -> Any:
    """The three-way merge of a JSON value: what each side changed from `base` is kept;
    where both changed the same thing differently, `ours` is."""
    if ours == theirs or base == theirs:
        return ours
    if base == ours:
        return theirs
    if isinstance(ours, dict) and isinstance(theirs, dict):
        old = base if isinstance(base, dict) else {}
        merged = {}
        for key in [*ours, *(key for key in theirs if key not in ours)]:
            value = _merge_value(old.get(key, _MISSING), ours.get(key, _MISSING),
                                 theirs.get(key, _MISSING))
            if value is not _MISSING:
                merged[key] = value
        return merged
    if isinstance(ours, list) and isinstance(theirs, list):
        old = base if isinstance(base, list) else []
        fields = _identity(old, ours, theirs)
        if fields is None:
            # Plain values: ours, less what theirs removed, with what theirs added.
            kept = [item for item in ours if item not in old or item in theirs]
            return kept + [item for item in theirs if item not in old and item not in kept]
        indexed = [{_key(item, fields): item for item in values}
                   for values in (old, ours, theirs)]
        was, mine, other = indexed
        result = []
        for key in [*mine, *(key for key in other if key not in mine)]:
            value = _merge_value(was.get(key, _MISSING), mine.get(key, _MISSING),
                                 other.get(key, _MISSING))
            if value is not _MISSING:
                result.append(value)
        return result
    return ours


def merge_contracts(base: dict[str, bytes], ours: dict[str, bytes],
                    theirs: dict[str, bytes]) -> dict[str, bytes | None]:
    """The files of the contract directory (by name, without components/) that combine
    what both sides changed since `base`: the requirements, the API model, the language
    and the architecture's structure merged item by item, keeping both sides' additions
    and changes, and the requirements document and the OpenAPI document rendered from
    the merged lists. Only files both sides changed differently are returned; any other,
    and one that cannot be read as the contract, is left to a merge of its lines."""
    result: dict[str, bytes | None] = {}

    def changed_on_both(name: str) -> bool:
        old, mine, other = base.get(name), ours.get(name), theirs.get(name)
        return (mine is not None and other is not None and mine != other
                and old != mine and old != other)

    def parse(files: dict[str, bytes], name: str) -> Any:
        data = files.get(name)
        return _MISSING if data is None else json.loads(data.decode())

    def final(name: str) -> Any:
        """The merged JSON of `name`, as combined here or as the plain merge keeps it."""
        if name in result:
            return json.loads((result[name] or b'null').decode())
        return parse(theirs if ours.get(name) == base.get(name) else ours, name)

    for name in _JSON_FILES:
        if not changed_on_both(name):
            continue
        try:
            merged = _merge_value(parse(base, name), parse(ours, name), parse(theirs, name))
            text = _json(merged)
            if name == ARCHITECTURE_JSON_FILENAME:
                try:
                    text = render_architecture_json(normalize_structure(merged))
                except (ValueError, TypeError):
                    pass
        except (ValueError, UnicodeDecodeError):
            continue
        result[name] = text.encode()
    if changed_on_both(OPENAPI_FILENAME):
        try:
            api = final(API_FILENAME)
            if api is not _MISSING:
                result[OPENAPI_FILENAME] = render_openapi(api).encode()
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
            pass
    if changed_on_both(NARRATIVE_FILENAME):
        try:
            requirements = final(REQUIREMENTS_FILENAME)
            narratives = [
                requirements_narrative(files[NARRATIVE_FILENAME].decode(),
                                       parse(files, REQUIREMENTS_FILENAME))
                if NARRATIVE_FILENAME in files and REQUIREMENTS_FILENAME in files else None
                for files in (base, ours, theirs)]
            old, mine, other = narratives
            if requirements is not _MISSING and mine is not None:
                narrative = other if other is not None and mine == old else mine
                result[NARRATIVE_FILENAME] = render_requirements_markdown(
                    requirements, narrative).encode()
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
            pass
    return result


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
