"""Host-only reading and checking of a page contract: the pages of a part's web client at
which criteria that only a laid-out page shows are observed in a real browser
(plans/browser-route.md, section 3.2).

A page contract lives in `contracts/parts/P/browser/` and holds:

- `pages.json`: `{"api_prefix": "/api", "pages": [PAGE, …]}`, each PAGE
  `{"entry": "/…", "operations": [operationId, …], "elements": [ELEMENT, …],
  "actions": [{"target": ELEMENT, "input": INPUT, "shows": [ELEMENT, …],
  "operations": [operationId, …]}, …]}`, an ELEMENT `{"role": …, "name"?: …}` or
  `{"testid": …}`;
- `fixtures/<operationId>[.<case>].json`: `{"status": …, "body": …}`, valid against the
  operation's response schema in the API's OpenAPI document;
- `viewports.json`: `{name: {"width": …, "height": …, "input": "mouse" | "touch",
  "scale"?: 1–3}}`.
"""
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app import docker_agents
from app.agent_workflows.host import build_image, copy_file, copy_regular_files
from app.asyncio_utils import run_sync
from app.images import PLAYWRIGHT_IMAGE
from app.languages import BrowserContract
from app.routing.models import (
    VIEWPORT, Element, PageContractError, element_key, read_element, read_entry,
)

logger = logging.getLogger(__name__)

PAGES_FILE = 'pages.json'
VIEWPORTS_FILE = 'viewports.json'
FIXTURES_DIR = 'fixtures'
# Written by the host beside a part's routed page contract, and into the runners' build
# contexts: the API's operations, by which the runners name the page's requests.
OPERATIONS_FILE = 'operations.json'
# The elements a change adds to a page, which the conformance check expects absent.
ABSENT_FILE = 'absent.json'
MAX_FILE_BYTES = 256 * 1024
MAX_FIXTURES = 200
MAX_PAGES = 30
MAX_ELEMENTS = 100
MAX_ACTIONS = 30
MAX_VIEWPORTS = 8
OPERATION = re.compile(r'[A-Za-z0-9_.-]{1,120}')
CASE = re.compile(r'[A-Za-z0-9_-]{1,60}')
INPUTS = ('reveal', 'hover', 'tap', 'click', 'focus')
WIDTH = (280, 3840)
HEIGHT = (400, 2160)
SCALE = (1, 3)
METHODS = ('get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace')
CHECK_SECONDS = 420
# How the conformance check is confined: no network. Chromium starts several processes.
CONSTRAINTS = ('--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
               '--pids-limit=1024', '--memory=4g', '--cpus=2', '--shm-size=512m',
               '--user', '10001:10001')
MESSAGE_CHARS = 4000


def _elements(value: Any, where: str) -> list[Element]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ELEMENTS:
        raise PageContractError(f'{where} are not a list of at most {MAX_ELEMENTS} elements')
    elements = [read_element(item, f'An element of {where}') for item in value]
    keys = [element_key(item) for item in elements]
    if len(set(keys)) != len(keys):
        raise PageContractError(f'{where} name an element twice')
    return elements


def _operations(value: Any, where: str, known: dict[str, Any]) -> list[str]:
    if value is None:
        return []
    if (not isinstance(value, list) or len(value) > MAX_ELEMENTS
            or not all(isinstance(item, str) and OPERATION.fullmatch(item) for item in value)):
        raise PageContractError(f'The operations of {where} are not a list of operationIds')
    unknown = [item for item in value if item not in known]
    if unknown:
        raise PageContractError(f'{where} names operations the API does not have: '
                                f'{", ".join(unknown)[:400]}')
    return list(dict.fromkeys(value))


def _page(value: Any, known: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(value) <= {'entry', 'operations', 'elements',
                                                         'actions'}:
        raise PageContractError('A page is not an object of entry, operations, elements and '
                                'actions')
    entry = read_entry(value.get('entry'), 'A page')
    where = f'Page {entry}'
    page: dict[str, Any] = {
        'entry': entry,
        'operations': _operations(value.get('operations'), where, known),
        'elements': _elements(value.get('elements'), f'the elements of page {entry}'),
        'actions': [],
    }
    actions = value.get('actions') or []
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        raise PageContractError(f'The actions of page {entry} are not a list of at most '
                                f'{MAX_ACTIONS}')
    for action in actions:
        if (not isinstance(action, dict)
                or not set(action) <= {'target', 'input', 'shows', 'operations'}):
            raise PageContractError(f'An action of page {entry} is not an object of target, '
                                    'input, shows and operations')
        if action.get('input') not in INPUTS:
            raise PageContractError(f'An action of page {entry} has no input of '
                                    f'{", ".join(INPUTS)}')
        page['actions'].append({
            'target': read_element(action.get('target'), f'The target of an action of page {entry}'),
            'input': action['input'],
            'shows': _elements(action.get('shows'), f'the elements an action of page {entry} shows'),
            'operations': _operations(action.get('operations'), f'an action of page {entry}', known),
        })
    targets = [f'{action["input"]} {element_key(action["target"])}' for action in page['actions']]
    if len(set(targets)) != len(targets):
        raise PageContractError(f'Page {entry} declares an action twice')
    return page


def page_elements(page: dict[str, Any]) -> dict[str, Element]:
    """Every element a page declares, present on load or after an action, by key."""
    elements = {element_key(item): item for item in page['elements']}
    for action in page['actions']:
        elements.setdefault(element_key(action['target']), action['target'])
        for item in action['shows']:
            elements.setdefault(element_key(item), item)
    return elements


def read_viewports(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not value or len(value) > MAX_VIEWPORTS:
        raise PageContractError(f'{VIEWPORTS_FILE} is not an object of 1 to {MAX_VIEWPORTS} '
                                'viewports by name')
    viewports: dict[str, dict[str, Any]] = {}
    for name, viewport in value.items():
        if not VIEWPORT.fullmatch(name) or len(name) > 30:
            raise PageContractError(f'The viewport name {name[:40]!r} is not lowercase words')
        if not isinstance(viewport, dict) or not set(viewport) <= {'width', 'height', 'input',
                                                                    'scale'}:
            raise PageContractError(f'Viewport {name} is not an object of width, height, input '
                                    'and scale')
        width, height = viewport.get('width'), viewport.get('height')
        if (not isinstance(width, int) or isinstance(width, bool)
                or not WIDTH[0] <= width <= WIDTH[1]):
            raise PageContractError(f'Viewport {name} has no width of {WIDTH[0]} to {WIDTH[1]}')
        if (not isinstance(height, int) or isinstance(height, bool)
                or not HEIGHT[0] <= height <= HEIGHT[1]):
            raise PageContractError(f'Viewport {name} has no height of {HEIGHT[0]} to '
                                    f'{HEIGHT[1]}')
        if viewport.get('input') not in ('mouse', 'touch'):
            raise PageContractError(f'Viewport {name} has no input of mouse or touch')
        scale = viewport.get('scale', 1)
        if (not isinstance(scale, (int, float)) or isinstance(scale, bool)
                or not SCALE[0] <= scale <= SCALE[1]):
            raise PageContractError(f'Viewport {name} has no scale of {SCALE[0]} to {SCALE[1]}')
        viewports[name] = {'width': width, 'height': height, 'input': viewport['input'],
                           'scale': scale}
    return viewports


def api_operations(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The operations of an OpenAPI document by operationId: method, path and responses."""
    operations: dict[str, dict[str, Any]] = {}
    for path, item in (document.get('paths') or {}).items():
        if not isinstance(item, dict):
            continue
        for method in METHODS:
            operation = item.get(method)
            if isinstance(operation, dict) and isinstance(operation.get('operationId'), str):
                operations[operation['operationId']] = {
                    'method': method.upper(), 'path': path,
                    'responses': operation.get('responses') or {}}
    return operations


def operations_list(document: dict[str, Any]) -> list[dict[str, str]]:
    """What the runners match the page's requests with: OPERATIONS_FILE."""
    return [{'operationId': name, 'method': item['method'], 'path': item['path']}
            for name, item in sorted(api_operations(document).items())]


def read_document(text: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PageContractError('The API\'s OpenAPI document cannot be read') from exc
    if not isinstance(document, dict):
        raise PageContractError('The API\'s OpenAPI document is not an object')
    return document


def _response_schema(document: dict[str, Any], operation: dict[str, Any],
                     status: int) -> tuple[str | None, bool]:
    """The JSON pointer of the response schema of `status`, or None when the response has
    no JSON body, and whether the document has a response for `status` at all."""
    responses = operation['responses']
    for key in (str(status), f'{str(status)[0]}XX', 'default'):
        if key in responses:
            response = responses[key]
            pointer = f'/paths/{_escape(operation["path"])}/{operation["method"].lower()}' \
                      f'/responses/{_escape(key)}'
            while isinstance(response, dict) and isinstance(response.get('$ref'), str):
                pointer = response['$ref'].removeprefix('#')
                response = _resolve(document, pointer)
            content = response.get('content') if isinstance(response, dict) else None
            if not isinstance(content, dict):
                return None, True
            for media, value in content.items():
                if 'json' in media and isinstance(value, dict) and 'schema' in value:
                    return f'{pointer}/content/{_escape(media)}/schema', True
            return None, True
    return None, False


def _escape(token: str) -> str:
    return token.replace('~', '~0').replace('/', '~1')


def _resolve(document: dict[str, Any], pointer: str) -> Any:
    value: Any = document
    for token in pointer.split('/')[1:]:
        token = token.replace('~1', '/').replace('~0', '~')
        if not isinstance(value, dict) or token not in value:
            raise PageContractError(f'The API\'s OpenAPI document has no {pointer}')
        value = value[token]
    return value


def check_fixture(document: dict[str, Any], operations: dict[str, dict[str, Any]],
                  name: str, value: Any) -> None:
    """A fixture is a response its operation may give: `{status, body}`, the body valid
    against the operation's response schema for the status."""
    operation_id = name.removesuffix('.json').split('.', 1)[0]
    operation = operations.get(operation_id)
    if operation is None:
        raise PageContractError(f'The fixture {name} is of an operation the API does not have: '
                                f'{operation_id}')
    if not isinstance(value, dict) or set(value) != {'status', 'body'}:
        raise PageContractError(f'The fixture {name} is not {{"status", "body"}}')
    status = value['status']
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        raise PageContractError(f'The fixture {name} has no HTTP status')
    pointer, known = _response_schema(document, operation, status)
    if not known:
        raise PageContractError(f'The fixture {name} has status {status}, which {operation_id} '
                                'does not respond with')
    if pointer is None:
        if value['body'] is not None:
            raise PageContractError(f'The fixture {name} has a body, but {operation_id} '
                                    f'responds with none for {status}')
        return
    registry: Registry = Registry().with_resource(
        'urn:openapi', Resource.from_contents(document, default_specification=DRAFT202012))
    validator = Draft202012Validator({'$ref': f'urn:openapi#{pointer}'}, registry=registry)
    try:
        error = next(iter(sorted(validator.iter_errors(value['body']),
                                 key=lambda item: list(item.absolute_path))), None)
    except Exception as exc:  # an unresolvable reference of the document
        raise PageContractError(f'The response schema of {operation_id} cannot be checked: '
                                f'{str(exc)[:300]}') from exc
    if error is not None:
        at = '/'.join(str(item) for item in error.absolute_path) or 'the body'
        raise PageContractError(f'The fixture {name} does not match the response schema of '
                                f'{operation_id} for {status} at {at}: {error.message[:300]}')


def _files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        return {}
    files: dict[str, Path] = {}
    for item in sorted(root.rglob('*')):
        if item.is_symlink():
            raise PageContractError(f'The page contract holds a link: '
                                    f'{item.relative_to(root).as_posix()[:200]}')
        if item.is_file():
            files[item.relative_to(root).as_posix()] = item
    return files


def _json(file: Path, name: str) -> Any:
    if file.stat().st_size > MAX_FILE_BYTES:
        raise PageContractError(f'The page contract\'s {name} is too large')
    try:
        return json.loads(file.read_text(encoding='utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PageContractError(f'The page contract\'s {name} is not valid JSON') from exc


def _read(files: dict[str, Path], document: dict[str, Any]) -> dict[str, Any]:
    operations = api_operations(document)
    for name in (PAGES_FILE, VIEWPORTS_FILE):
        if name not in files:
            raise PageContractError(f'The page contract has no {name}')
    other = [path for path in files if path not in (PAGES_FILE, VIEWPORTS_FILE)
             and not (path.startswith(f'{FIXTURES_DIR}/') and path.count('/') == 1
                      and path.endswith('.json'))]
    if other:
        raise PageContractError(f'The page contract holds a file it may not: {other[0][:200]}')
    data = _json(files[PAGES_FILE], PAGES_FILE)
    if not isinstance(data, dict) or not set(data) <= {'api_prefix', 'pages'}:
        raise PageContractError(f'{PAGES_FILE} is not an object of api_prefix and pages')
    prefix = data.get('api_prefix', '')
    if not isinstance(prefix, str) or (prefix and not re.fullmatch(r'(/[A-Za-z0-9._~-]+)+',
                                                                    prefix)):
        raise PageContractError(f'The api_prefix of {PAGES_FILE} is not "" or a path such as '
                                '/api')
    items = data.get('pages')
    if not isinstance(items, list) or not items or len(items) > MAX_PAGES:
        raise PageContractError(f'{PAGES_FILE} has no list of 1 to {MAX_PAGES} pages')
    pages: dict[str, dict[str, Any]] = {}
    for item in items:
        page = _page(item, operations)
        if page['entry'] in pages:
            raise PageContractError(f'{PAGES_FILE} declares page {page["entry"]} twice')
        pages[page['entry']] = page
    fixtures: dict[str, Any] = {}
    names = [path for path in files if path.startswith(f'{FIXTURES_DIR}/')]
    if len(names) > MAX_FIXTURES:
        raise PageContractError(f'The page contract has more than {MAX_FIXTURES} fixtures')
    for path in names:
        name = path.split('/', 1)[1]
        stem = name.removesuffix('.json')
        operation, _, case = stem.partition('.')
        if not OPERATION.fullmatch(operation) or (case and not CASE.fullmatch(case)):
            raise PageContractError(f'The fixture {name} is not named '
                                    '<operationId>[.<case>].json')
        fixtures[name] = _json(files[path], path)
        check_fixture(document, operations, name, fixtures[name])
    for page in pages.values():
        named = [*page['operations'], *(item for action in page['actions']
                                        for item in action['operations'])]
        missing = sorted({item for item in named if f'{item}.json' not in fixtures})
        if missing:
            raise PageContractError(f'Page {page["entry"]} calls operations without a default '
                                    f'fixture: {", ".join(f"fixtures/{item}.json" for item in missing)[:400]}')
    viewports = read_viewports(_json(files[VIEWPORTS_FILE], VIEWPORTS_FILE))
    return {'api_prefix': prefix, 'pages': pages, 'fixtures': fixtures, 'viewports': viewports}


def _kept(published: dict[str, Any], read: dict[str, Any]) -> None:
    """A published page contract may be extended, never changed or reduced."""
    if published['api_prefix'] != read['api_prefix']:
        raise PageContractError(f'The published api_prefix of {PAGES_FILE} was changed')
    for entry, page in published['pages'].items():
        kept = read['pages'].get(entry)
        if kept is None:
            raise PageContractError(f'The published page {entry} was removed')
        for key in ('operations', 'elements'):
            lost = [item for item in page[key] if item not in kept[key]]
            if lost:
                raise PageContractError(f'Page {entry} lost published {key}: '
                                        f'{", ".join(element_key(item) if isinstance(item, dict) else item for item in lost)[:400]}')
        for action in page['actions']:
            same = next((item for item in kept['actions'] if item['target'] == action['target']
                         and item['input'] == action['input']), None)
            if same is None or any(item not in same['shows'] for item in action['shows']) \
                    or any(item not in same['operations'] for item in action['operations']):
                raise PageContractError(f'The published action {action["input"]} '
                                        f'{element_key(action["target"])} of page {entry} was '
                                        'changed or removed')
    for name, fixture in published['fixtures'].items():
        if read['fixtures'].get(name) != fixture:
            raise PageContractError(f'The published fixture {name} was changed or removed')
    for name, viewport in published['viewports'].items():
        if read['viewports'].get(name) != viewport:
            raise PageContractError(f'The published viewport {name} was changed or removed; a '
                                    'different size is a new name')


def read_page_contract(output: Path, published: Path, document: dict[str, Any],
                       target: Path | None = None) -> dict[str, Any]:
    """Read the agent's untrusted page contract in `output` against the API's OpenAPI
    `document`, and copy it to `target`. The published contract (`published`) is kept:
    only extended. Returns `api_prefix`, `pages` by entry, `fixtures` by name,
    `viewports` by name and the names of the `published_viewports`."""
    read = _read(_files(output), document)
    published_files = _files(published)
    if published_files:
        try:
            before = _read(published_files, document)
        except PageContractError as exc:
            raise PageContractError(f'The published page contract is unusable: {exc}') from exc
        _kept(before, read)
        read['published_viewports'] = sorted(before['viewports'])
    else:
        read['published_viewports'] = []
    if target is not None:
        target.mkdir(parents=True)
        for path, file in _files(output).items():
            (target / path).parent.mkdir(parents=True, exist_ok=True)
            copy_file(file, target / path)
    return read


def check_page_routes(routes: dict[str, dict[str, Any]], contract: dict[str, Any]) -> None:
    """Every `browser` route opens a page of the contract, at viewports of it, and uses
    elements it declares; every viewport the contract adds is named by some route, so
    none runs for nothing."""
    named: set[str] = set()
    for requirement, route in routes.items():
        page = route.get('page')
        if route.get('route') != 'browser' or page is None:
            continue
        declared = contract['pages'].get(page['entry'])
        if declared is None:
            raise PageContractError(f'The page of requirement {requirement} is not in '
                                    f'{PAGES_FILE}: {page["entry"]}')
        unknown = [name for name in page['viewports'] if name not in contract['viewports']]
        if unknown:
            raise PageContractError(f'The page of requirement {requirement} names viewports '
                                    f'{VIEWPORTS_FILE} does not have: {", ".join(unknown)}')
        named.update(page['viewports'])
        elements = page_elements(declared)
        missing = [element_key(item) for item in page['roles']
                   if element_key(item) not in elements]
        if missing:
            raise PageContractError(f'The page of requirement {requirement} uses elements page '
                                    f'{page["entry"]} does not declare: '
                                    f'{", ".join(missing)[:400]}')
    unused = sorted(set(contract['viewports']) - named - set(contract['published_viewports']))
    if unused:
        raise PageContractError(f'No route names the viewports {", ".join(unused)}, which would '
                                'run for nothing')


def absent_elements(routes: dict[str, dict[str, Any]]) -> list[Element]:
    """The elements the change adds to pages, which the client lacks yet."""
    added: dict[str, Element] = {}
    for route in routes.values():
        page = route.get('page') if route.get('route') == 'browser' else None
        for item in (page or {}).get('added') or []:
            added.setdefault(element_key(item), item)
    return list(added.values())


def _context(contract: BrowserContract, repository: Path, browser: Path, operations: Any,
             absent: list[Element], context: Path) -> None:
    shutil.copytree(contract.conformance, context)
    copy_regular_files(repository, context / 'repository', include=lambda relative: True)
    # Validated already: regular files only.
    shutil.copytree(browser, context / 'contract')
    checks = context / 'checks'
    checks.mkdir()
    (checks / OPERATIONS_FILE).write_text(json.dumps(operations), encoding='utf-8')
    (checks / ABSENT_FILE).write_text(json.dumps(absent), encoding='utf-8')


async def check_page_conformance(contract: BrowserContract, repository: Path, project: str,
                                 browser: Path, operations: Any,
                                 absent: list[Element], work: Path,
                                 log_dir: Path) -> None:
    """Check, without a network, that the part at `project` of `repository`, built,
    shows the page contract in `browser`: every declared element is found once a page is
    idle at every viewport, but the `absent` ones, which must not be, and every API call
    is declared and answered by a fixture. Raises RuntimeError naming the differences
    otherwise, and docker_agents.ImageUnavailable when the runner's base image cannot be
    pulled."""
    await docker_agents.ensure_image(PLAYWRIGHT_IMAGE)
    context = work / 'page-conformance'
    await run_sync(_context, contract, repository, browser, operations, absent, context)
    try:
        image = await build_image(context, 'page conformance', log_dir / 'page-conformance-build.log',
                                  build_args=contract.build_args(project), pull=False)
    except RuntimeError as exc:
        raise RuntimeError('The web client the page contract describes could not be built for '
                           f'its check: {str(exc)[-MESSAGE_CHARS:]}') from exc
    try:
        await docker_agents.docker('run', '--rm', *CONSTRAINTS, image, timeout=CHECK_SECONDS,
                                   log_path=log_dir / 'page-conformance.log')
    except RuntimeError as exc:
        raise RuntimeError('The page contract does not match the web client: '
                           f'{str(exc).removeprefix("Docker run failed: ")[-MESSAGE_CHARS:]}'
                           ) from exc
    finally:
        try:
            await docker_agents.docker('image', 'rm', '-f', image, timeout=60)
        except Exception:
            logger.warning('Could not remove the page conformance image %s', image)
    logger.info('Page contract conforms (project=%s)', project)
