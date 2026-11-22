"""The browser route: criteria only a laid-out page shows, proved by browser tests of a
part's built web client in headless Chromium (plans/browser-route.md). The runners
themselves run in tests/test_browser_docker.py, with real Docker."""
# Assertions read optional fields directly: a None there fails the test anyway.
# pyright: reportOptionalMemberAccess=false
import asyncio
import io
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.images import PLAYWRIGHT_IMAGE, PLAYWRIGHT_VERSION, PREPULLED_IMAGES
from app.languages import get_language, get_part_language
from app.languages.composite import CompositeSuite
from app.languages.typescript.browser import BROWSER_SUITE, CONTRACT_RUNNER
from app.routing import activities as routing
from app.routing.agent import prompt
from app.routing.browser import (
    PageContractError, absent_elements, check_page_routes, operations_list, read_document,
    read_page_contract,
)
from app.routing.models import (
    RequirementRoute, added_seams, judge_routes, read_page, read_routes,
)
from app.routing.parts import (
    SERVICE_PART, ComponentPart, component_parts, component_suite, routed_parts,
)
from app.test_generation import activities as contract_tests
from app.test_generation import components
from app.verification import activities as verification

PYTHON = get_language('python')
TYPESCRIPT = get_part_language('typescript')
SERVICE = ComponentPart(SERVICE_PART, '.', PYTHON)
FRONTEND = ComponentPart('frontend', 'frontend', TYPESCRIPT)
BROWSER = ComponentPart('frontend', 'frontend', TYPESCRIPT, browser=True)
BACKEND = Path(__file__).resolve().parent.parent
IMAGE = 'sha256:' + 'e' * 64

DOCUMENT = read_document('''
openapi: 3.1.0
info: {title: Rooms, version: 1.0.0}
paths:
  /rooms/{room}:
    get:
      operationId: getRoom
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema: {$ref: '#/components/schemas/Room'}
        '404':
          description: Not found
  /me:
    get:
      operationId: getMe
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema: {type: object, properties: {login: {type: string}}, required: [login]}
components:
  schemas:
    Room:
      type: object
      properties:
        name: {type: string}
        requirements: {type: array, items: {type: string}}
      required: [name]
''')
PAGES = {'api_prefix': '/api', 'pages': [{
    'entry': '/rooms/1', 'operations': ['getMe', 'getRoom'],
    'elements': [{'role': 'button', 'name': 'FR-1'}],
    'actions': [{'target': {'role': 'button', 'name': 'FR-1'}, 'input': 'reveal',
                 'shows': [{'role': 'tooltip'}, {'testid': 'popup'}]}]}]}
VIEWPORTS = {'desktop': {'width': 1280, 'height': 800, 'input': 'mouse'},
             'narrow': {'width': 390, 'height': 844, 'input': 'touch'}}
FIXTURES = {'getMe.json': {'status': 200, 'body': {'login': 'ann'}},
            'getRoom.json': {'status': 200, 'body': {'name': 'Blue', 'requirements': ['FR-1']}},
            'getRoom.missing.json': {'status': 404, 'body': None}}
PAGE = {'part': 'frontend', 'entry': '/rooms/1', 'viewports': ['desktop', 'narrow'],
        'roles': [{'role': 'button', 'name': 'FR-1'}, {'role': 'tooltip'}]}
POPUP_TEST = '''import { test, fixture, reveal, settle, expectFullyVisible } from './support';

test.describe('the requirement popup', () => {
  test.beforeEach(async ({ page }) => {
    await fixture(page, 'getMe');
    await fixture(page, 'getRoom');
    await page.goto('/rooms/1');
  });

  // requirement: FR-171
  test('is fully visible', { tag: ['@desktop', '@narrow'] }, async ({ page }) => {
    await reveal(page.getByRole('button', { name: 'FR-1', exact: true }));
    await settle(page);
    await expectFullyVisible(page.getByRole('tooltip'));
  });
});
'''
POPUP_CASES = ['tests/popup.spec.ts#desktop > the requirement popup > is fully visible',
               'tests/popup.spec.ts#narrow > the requirement popup > is fully visible']


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


def contract_files(pages=None, viewports=None, fixtures=None) -> dict[str, str]:
    return {'pages.json': json.dumps(PAGES if pages is None else pages),
            'viewports.json': json.dumps(VIEWPORTS if viewports is None else viewports),
            **{f'fixtures/{name}': json.dumps(value)
               for name, value in (FIXTURES if fixtures is None else fixtures).items()}}


# ---- pinning ----

def test_the_playwright_package_and_the_runner_image_are_the_same_release():
    """Upgrading Playwright is a change of both: the runner image's tag names the version
    of @playwright/test every runner project installs, exactly."""
    assert PLAYWRIGHT_IMAGE in PREPULLED_IMAGES
    tag = re.fullmatch(r'mcr\.microsoft\.com/playwright:v([\d.]+)-noble@sha256:[0-9a-f]{64}',
                       PLAYWRIGHT_IMAGE)
    assert tag, 'the image is pinned by tag and digest'
    for project in (BROWSER_SUITE.template, CONTRACT_RUNNER):
        manifest = json.loads((project / 'package.json').read_text())
        lock = json.loads((project / 'package-lock.json').read_text())
        version = manifest['devDependencies']['@playwright/test']
        assert version == tag.group(1) == PLAYWRIGHT_VERSION, project.name
        assert lock['packages']['node_modules/@playwright/test']['version'] == version
        assert lock['packages']['node_modules/playwright-core']['version'] == version
    # Both runners are built on the pinned image, which the build arguments name.
    for project in (BROWSER_SUITE.template, CONTRACT_RUNNER):
        assert 'FROM ${BASE_IMAGE}' in (project / 'Dockerfile').read_text()
    assert BROWSER_SUITE.build_args({'project': 'frontend'}) == {
        'PROJECT': 'frontend', 'BASE_IMAGE': PLAYWRIGHT_IMAGE,
        'PLAYWRIGHT_VERSION': PLAYWRIGHT_VERSION}
    # The suite's author cannot install another release: the runner's build refuses it.
    dockerfile = (BROWSER_SUITE.template / 'Dockerfile').read_text()
    assert 'test "$installed" = "$PLAYWRIGHT_VERSION"' in dockerfile
    assert 'CLASSNAME_PREFIX=${CLASSNAME_PREFIX}' in dockerfile


def test_the_runner_bounds_the_whole_run():
    assert 'timeout --signal=TERM --kill-after=10 600' in (
        BROWSER_SUITE.template / 'run-tests.sh').read_text()


# ---- the page contract ----

def test_a_page_contract_is_read_against_the_api(tmp_path):
    write(tmp_path / 'browser', contract_files())
    read = read_page_contract(tmp_path / 'browser', tmp_path / 'none', DOCUMENT,
                              tmp_path / 'kept')
    assert read['api_prefix'] == '/api' and list(read['pages']) == ['/rooms/1']
    assert read['viewports']['narrow'] == {'width': 390, 'height': 844, 'input': 'touch',
                                           'scale': 1}
    assert (tmp_path / 'kept/fixtures/getRoom.missing.json').is_file()
    assert operations_list(DOCUMENT) == [
        {'operationId': 'getMe', 'method': 'GET', 'path': '/me'},
        {'operationId': 'getRoom', 'method': 'GET', 'path': '/rooms/{room}'}]


def _pages(**changes) -> dict:
    return {**PAGES, 'pages': [{**PAGES['pages'][0], **changes}]}


@pytest.mark.parametrize('files, error', [
    # A fixture that breaks its operation's response schema.
    (contract_files(fixtures={**FIXTURES, 'getRoom.json': {'status': 200, 'body': {'id': 1}}}),
     "getRoom.json does not match the response schema of getRoom for 200 at the body: "
     "'name' is a required property"),
    (contract_files(fixtures={**FIXTURES, 'getRoom.json': {
        'status': 200, 'body': {'name': 'Blue', 'requirements': [1]}}}),
     'at requirements/0'),
    (contract_files(fixtures={**FIXTURES, 'getRoom.json': {'status': 500, 'body': None}}),
     'status 500, which getRoom does not respond with'),
    (contract_files(fixtures={**FIXTURES, 'getRoom.missing.json': {'status': 404, 'body': {}}}),
     'has a body, but getRoom responds with none for 404'),
    # Operations the API does not have.
    (contract_files(fixtures={**FIXTURES, 'listRooms.json': {'status': 200, 'body': []}}),
     'of an operation the API does not have: listRooms'),
    (contract_files(pages=_pages(operations=['getMe', 'getRoom', 'listRooms'])),
     'names operations the API does not have: listRooms'),
    (contract_files(fixtures={'getMe.json': FIXTURES['getMe.json']}),
     'calls operations without a default fixture: fixtures/getRoom.json'),
    # Pages and elements.
    (contract_files(pages=_pages(entry='rooms/1')), 'has no entry'),
    (contract_files(pages=_pages(entry='/../secrets')), 'has no entry'),
    (contract_files(pages=_pages(elements=[{'role': 'Button'}])), 'invalid ARIA role'),
    (contract_files(pages=_pages(elements=[{'role': 'button', 'testid': 'x'}])),
     'neither'),
    (contract_files(pages={**PAGES, 'pages': PAGES['pages'] * 2}), 'declares page /rooms/1 twice'),
    # Viewports.
    (contract_files(viewports={'Desktop': VIEWPORTS['desktop']}), 'is not lowercase words'),
    (contract_files(viewports={'tiny': {'width': 200, 'height': 800, 'input': 'mouse'}}),
     'no width of 280 to 3840'),
    (contract_files(viewports={'tall': {'width': 800, 'height': 3000, 'input': 'mouse'}}),
     'no height of 400 to 2160'),
    (contract_files(viewports={'pen': {'width': 800, 'height': 800, 'input': 'pen'}}),
     'no input of mouse or touch'),
    (contract_files(viewports={'hidpi': {**VIEWPORTS['desktop'], 'scale': 4}}),
     'no scale of 1 to 3'),
    # Anything else.
    ({**contract_files(), 'notes.md': 'x'}, 'holds a file it may not: notes.md'),
    ({'pages.json': json.dumps(PAGES)}, 'has no viewports.json'),
])
def test_an_unusable_page_contract_is_refused(tmp_path, files, error):
    write(tmp_path / 'browser', files)
    with pytest.raises(PageContractError, match=re.escape(error)):
        read_page_contract(tmp_path / 'browser', tmp_path / 'none', DOCUMENT)


@pytest.mark.parametrize('files, error', [
    (contract_files(), None),
    # Extended: a new page, element, fixture and viewport.
    (contract_files(
        pages={**PAGES, 'pages': [
            {**PAGES['pages'][0], 'elements': [*PAGES['pages'][0]['elements'],
                                               {'testid': 'room'}]},
            {'entry': '/', 'operations': ['getMe']}]},
        viewports={**VIEWPORTS, 'tablet': {'width': 768, 'height': 1024, 'input': 'touch'}},
        fixtures={**FIXTURES, 'getMe.admin.json': {'status': 200, 'body': {'login': 'root'}}}),
     None),
    (contract_files(pages=_pages(elements=[])), 'Page /rooms/1 lost published elements'),
    (contract_files(pages={**PAGES, 'pages': [{'entry': '/', 'operations': ['getMe']}]}),
     'The published page /rooms/1 was removed'),
    (contract_files(pages=_pages(actions=[])), 'The published action reveal'),
    (contract_files(pages={**PAGES, 'api_prefix': '/v2'}), 'published api_prefix'),
    (contract_files(fixtures={**FIXTURES, 'getMe.json': {'status': 200,
                                                         'body': {'login': 'bob'}}}),
     'The published fixture getMe.json was changed'),
    (contract_files(viewports={**VIEWPORTS, 'narrow': {**VIEWPORTS['narrow'], 'width': 400}}),
     'The published viewport narrow was changed or removed; a different size is a new name'),
])
def test_a_published_page_contract_is_only_extended(tmp_path, files, error):
    write(tmp_path / 'published', contract_files())
    write(tmp_path / 'browser', files)
    if error is None:
        read = read_page_contract(tmp_path / 'browser', tmp_path / 'published', DOCUMENT)
        assert read['published_viewports'] == ['desktop', 'narrow']
    else:
        with pytest.raises(PageContractError, match=re.escape(error)):
            read_page_contract(tmp_path / 'browser', tmp_path / 'published', DOCUMENT)


@pytest.mark.parametrize('page, published, error', [
    (PAGE, [], None),
    ({**PAGE, 'entry': '/'}, [], 'is not in pages.json: /'),
    ({**PAGE, 'viewports': ['desktop', 'tablet']}, [], 'viewports.json does not have: tablet'),
    ({**PAGE, 'roles': [{'role': 'dialog'}]}, [], 'does not declare: role=dialog'),
    # A viewport no route names would run for nothing, unless it was published.
    ({**PAGE, 'viewports': ['narrow']}, [], 'No route names the viewports desktop'),
    ({**PAGE, 'viewports': ['narrow']}, ['desktop'], None),
])
def test_each_browser_route_uses_what_its_page_contract_declares(tmp_path, page, published,
                                                                  error):
    write(tmp_path / 'browser', contract_files())
    contract = read_page_contract(tmp_path / 'browser', tmp_path / 'none', DOCUMENT)
    contract['published_viewports'] = published
    routes = {'FR-171': {'route': 'browser', 'reason': 'r', 'page': page}}
    if error is None:
        check_page_routes(routes, contract)
    else:
        with pytest.raises(PageContractError, match=re.escape(error)):
            check_page_routes(routes, contract)


# ---- routes ----

def test_a_browser_route_names_a_page_of_a_part_with_browser_tests():
    data = {'routes': [{'requirement_id': 'FR-171', 'route': 'browser', 'reason': 'Cut off.',
                        'page': {**PAGE, 'added': [{'role': 'tooltip'}]}}]}
    routes = read_routes(data, ['FR-171'], browser_parts=('frontend',))
    assert routes['FR-171']['page'] == {**PAGE, 'added': [{'role': 'tooltip'}]}
    with pytest.raises(RuntimeError, match='names no part with browser tests: frontend; the '
                                           'parts with them are none'):
        read_routes(data, ['FR-171'])
    with pytest.raises(RuntimeError, match='no valid route'):
        read_routes(data, ['FR-171'], routes=('contract', 'component', 'unobservable'),
                    browser_parts=('frontend',))


@pytest.mark.parametrize('page, error', [
    ({**PAGE, 'entry': 'rooms'}, 'has no entry'),
    ({**PAGE, 'viewports': []}, 'names no viewports'),
    ({**PAGE, 'viewports': ['Wide']}, 'names no viewports'),
    ({**PAGE, 'roles': []}, 'names no roles'),
    ({**PAGE, 'roles': [{'name': 'FR-1'}]}, 'neither'),
    ({**PAGE, 'added': [{'testid': 'popup'}]}, 'adds elements its roles do not name: '
                                               'testid=popup'),
])
def test_an_unusable_page_is_refused(page, error):
    with pytest.raises(RuntimeError, match=re.escape(error)):
        read_page('FR-171', page, ('frontend',))


def test_the_elements_a_page_lacks_are_added_first_as_a_seam_is():
    routes = {
        'FR-171': {'route': 'browser', 'page': {**PAGE, 'added': [{'role': 'tooltip'}]}},
        'FR-172': {'route': 'browser', 'page': {**PAGE, 'roles': [{'testid': 'popup'}],
                                                'added': [{'testid': 'popup'}]}},
        'FR-173': {'route': 'browser', 'page': PAGE},
    }
    assert added_seams(routes) == {'frontend/page:/rooms/1': ['role=tooltip', 'testid=popup']}
    assert absent_elements(routes) == [{'role': 'tooltip'}, {'testid': 'popup'}]


def unobservable(reason: str) -> dict[str, RequirementRoute]:
    return {'FR-171': {'route': 'unobservable', 'reason': reason, 'source': 'routing'},
            'FR-2': {'route': 'contract', 'reason': 'GET /rooms', 'source': 'routing'}}


def test_the_stop_message_names_what_could_show_a_criterion_and_how_to_measure_layout():
    status, message = judge_routes(unobservable('Only the server is observable.'))
    assert status == 'unobservable' and message is not None
    assert "as behaviour the service's HTTP API shows" in message
    assert 'window sizes' not in message
    status, message = judge_routes(unobservable('The popup is cut off by its panel, which '
                                                'jsdom does not lay out.'), parts=True)
    assert message is not None
    assert ('as behaviour the HTTP API, a seam of the code or a page of the web client shows'
            in message)
    assert ('For a criterion about how a page looks or fits: Say which element, at which '
            'window sizes') in message
    assert '"looks right" is not a criterion' in message


# ---- parts ----

def test_a_vite_client_is_a_part_with_browser_tests(tmp_path):
    write(tmp_path, {
        'Dockerfile': 'FROM python:3.12-slim', 'requirements.txt': 'temporalio\n',
        'src/service/rooms.py': '',
        'frontend/package.json': json.dumps({'devDependencies': {'typescript': '5',
                                                                 'vite': '8'}}),
        'frontend/package-lock.json': '{}', 'frontend/tsconfig.json': '{}',
        'frontend/index.html': '<!doctype html>',
        # A TypeScript library without a page has component tests alone.
        'library/package.json': json.dumps({'devDependencies': {'typescript': '5'}}),
        'library/package-lock.json': '{}', 'library/tsconfig.json': '{}',
    })
    parts = component_parts(PYTHON, tmp_path)
    assert [(part.key, part.browser) for part in parts] == [
        ('service', False), ('frontend', False), ('frontend~browser', True),
        ('library', False)]
    browser = parts[2]
    assert browser.contract is None and browser.browser_contract is not None
    assert browser.contract_dir().as_posix() == 'parts/frontend/browser'
    assert browser.published_dir().as_posix() == 'contracts/parts/frontend/browser'
    assert browser.tests_dir().as_posix() == 'frontend/browser-tests'
    assert browser.snapshot_dir().as_posix() == 'browser/frontend'
    assert browser.record() == {'name': 'frontend', 'root': 'frontend',
                                'language': 'typescript', 'kind': 'browser'}
    # The routing tells its agent of each part once.
    assert routing.describe_parts(parts) == [
        {'name': 'service', 'root': '.', 'language': PYTHON.label},
        {'name': 'frontend', 'root': 'frontend', 'language': 'TypeScript', 'browser': True},
        {'name': 'library', 'root': 'library', 'language': 'TypeScript'}]
    assert routed_parts(PYTHON, {'parts': [FRONTEND.record(), BROWSER.record()]}) == [
        FRONTEND, BROWSER]
    with pytest.raises(ValueError, match='names a part twice'):
        routed_parts(PYTHON, {'parts': [BROWSER.record(), BROWSER.record()]})
    with pytest.raises(ValueError, match='unknown kind'):
        routed_parts(PYTHON, {'parts': [{**BROWSER.record(), 'kind': 'native'}]})


def test_the_routing_offers_the_browser_route_only_for_such_a_part(tmp_path):
    routes = {'routes': [{'requirement_id': 'FR-171', 'route': 'browser', 'reason': 'Cut off.',
                          'page': PAGE}]}
    (tmp_path / 'routes.json').write_text(json.dumps(routes))
    read = routing.read_output(tmp_path, ['FR-171'], PYTHON, [SERVICE, FRONTEND, BROWSER])
    assert read['FR-171']['page'] == PAGE
    with pytest.raises(RuntimeError, match='no valid route'):
        routing.read_output(tmp_path, ['FR-171'], PYTHON, [SERVICE, FRONTEND])
    language = {'label': 'Python', 'components': 'C', 'part_components': {'typescript': 'T'},
                'part_browser': {'typescript': 'B'}}
    text = prompt(language)
    assert '"contract", "component", "browser" or "unobservable"' in text
    assert 'only the laid-out' in text and '"page": {"part": "…", "entry": "/…"' in text
    assert '"browser"' not in prompt({**language, 'part_browser': {}})


# ---- the browser suite ----

def test_each_browser_test_is_named_once_per_viewport_it_is_tagged_with(tmp_path):
    write(tmp_path, {'tests/popup.spec.ts': POPUP_TEST, 'tests/support/index.ts': 'export {}'})
    assert sorted(BROWSER_SUITE.test_cases(tmp_path)) == POPUP_CASES
    assert BROWSER_SUITE.untracked_tests(tmp_path) == []
    # As the runner's reporter names them.
    assert BROWSER_SUITE.case_id('tests/popup.spec.ts#narrow',
                                 'the requirement popup > is fully visible') == POPUP_CASES[1]
    assert BROWSER_SUITE.viewport_of(POPUP_CASES[1]) == 'narrow'


@pytest.mark.parametrize('source, problem', [
    ("test('a', async () => {});", "the test 'a' has no viewport tag"),
    ("test(`a ${b}`, { tag: '@narrow' }, async () => {});", 'named by an expression'),
    ("test('a', { tag: narrow }, async () => {});", 'tag is not given as a string literal'),
    ("test('a', { tag: ['@narrow', x] }, async () => {});", 'tag is not given as string literals'),
    ("test('a', { tag }, async () => {});", 'tag is not given as a string literal'),
    ("test('a', { ...details }, async () => {});", 'details spread an object'),
    ("test('a', { tag: '@Narrow' }, async () => {});", "is not @ and a viewport's name"),
    ("test('a @narrow', { tag: '@narrow' }, async () => {});", 'has `@` in its name'),
])
def test_a_browser_test_that_cannot_be_tracked_is_named(tmp_path, source, problem):
    write(tmp_path, {'tests/a.spec.ts': f"import {{ test }} from './support';\n{source}\n"})
    untracked, = BROWSER_SUITE.untracked_tests(tmp_path)
    assert problem in untracked
    assert BROWSER_SUITE.test_cases(tmp_path) == {}


def test_tags_of_a_describe_block_hold_for_its_tests(tmp_path):
    write(tmp_path, {'tests/a.spec.ts': '''import { test } from './support';
test.describe('popup', { tag: '@narrow' }, () => {
  test.describe.configure({ mode: 'serial' });
  test.skip(({ browserName }) => browserName !== 'chromium');
  test('fits', { tag: ['@desktop'] }, async () => {});
  test.fixme('later', async () => {});
});
'''})
    assert sorted(BROWSER_SUITE.test_cases(tmp_path)) == [
        'tests/a.spec.ts#desktop > popup > fits', 'tests/a.spec.ts#narrow > popup > fits',
        'tests/a.spec.ts#narrow > popup > later']


def test_the_helpers_are_always_the_templates(tmp_path):
    write(tmp_path / 'author', {'tests/popup.spec.ts': POPUP_TEST,
                                'tests/support/index.ts': 'export const expectFullyVisible = () => {};',
                                'package.json': '{}', 'playwright.config.mjs': 'untrusted'})
    contract_tests.copy_test_suite(BROWSER_SUITE, tmp_path / 'author', tmp_path / 'copy')
    assert (tmp_path / 'copy/tests/support/index.ts').read_text() == (
        BROWSER_SUITE.template / 'tests/support/index.ts').read_text()
    assert not (tmp_path / 'copy/playwright.config.mjs').exists()


def composite() -> CompositeSuite:
    suite = component_suite(PYTHON, [SERVICE, FRONTEND, BROWSER])
    assert isinstance(suite, CompositeSuite)
    return suite


def test_the_browser_tests_are_a_member_of_the_delivery_suite_named_after_their_part():
    suite = composite()
    service, frontend, browser = suite.members
    assert browser.name == 'frontend~browser' and browser.browser
    assert browser.directory.as_posix() == 'browser/frontend'
    assert browser.prefix == 'frontend~browser:'
    assert 'the browser tests of part frontend (TypeScript) at /workspace/tests/browser/frontend' \
        in suite.instructions
    assert '/workspace/contracts/parts/P/browser' in suite.instructions
    assert suite.part_of('frontend~browser:tests/popup.spec.ts#narrow') == 'frontend~browser'
    assert suite.part_of('frontend:tests/manifest.test.ts') == 'frontend'
    assert suite.part_of('tests.test_rooms') == 'service'
    assert suite.case_id('frontend~browser:tests/popup.spec.ts#narrow',
                         'the requirement popup > is fully visible') == (
        f'frontend~browser:{POPUP_CASES[1]}')
    assert 'browser/frontend/package-lock.json' in suite.required


# ---- building and running the browser runner beside the others ----

@pytest.fixture
def session(tmp_path, monkeypatch):
    """A Python service with a Vite web client, whose criterion the routing sent to a page
    of the client, with its page contract checked."""
    for module in (components, verification, contract_tests):
        monkeypatch.setattr(module, 'GENERATED_CODE_DIR', tmp_path)
    session_dir = tmp_path / 'session'
    write(session_dir, {
        'repository/Dockerfile': 'FROM python:3.12-slim',
        'repository/src/service/rooms.py': 'PRIVATE SERVICE SOURCE',
        'repository/frontend/package.json': json.dumps(
            {'devDependencies': {'typescript': '5', 'vite': '8'}}),
        'repository/frontend/package-lock.json': '{}',
        'repository/frontend/tsconfig.json': '{}',
        'repository/frontend/index.html': '<!doctype html>',
        'repository/frontend/src/popup.ts': 'PRIVATE CLIENT SOURCE',
        'routing/parts/frontend/operations.json': json.dumps(operations_list(DOCUMENT)),
        **{f'routing/parts/frontend/browser/{name}': text
           for name, text in contract_files().items()},
        'routing/routes.json': json.dumps({'routes': {}, 'parts': [BROWSER.record()]}),
    })
    return session_dir


CONTEXT = {'requirements': [], 'requirements_document': '# Contract', 'language': 'python',
           'brief': {'requirements': [{'id': 'FR-171', 'category': 'functional', 'title': 'T',
                                       'description': 'D', 'priority': 'must'}]},
           'routes': {'FR-171': {'route': 'browser', 'reason': 'Cut off by its panel.',
                                 'page': PAGE}}}
AUTHOR = {'browser/frontend/tests/popup.spec.ts': POPUP_TEST,
          'browser/frontend/package.json': (BROWSER_SUITE.template / 'package.json').read_text(),
          'browser/frontend/package-lock.json': '{}'}


def fake_docker(monkeypatch, written: dict[str, str], sent: dict, calls: list):
    async def docker(*args, **kwargs):
        if args[0] == 'build' and '--iidfile' in args:
            context = Path(args[-1])
            calls.append(('build', args, {item.relative_to(context).as_posix(): item.read_text()
                                          for item in context.rglob('*') if item.is_file()}))
            Path(args[args.index('--iidfile') + 1]).write_text(IMAGE)
            return ''
        calls.append(args)
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode(errors='replace')
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/tests/.'):
            write(Path(args[2]), written)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)


async def test_the_author_writes_browser_tests_from_the_page_contract_alone(session, monkeypatch):
    sent: dict = {}
    calls: list = []
    fake_docker(monkeypatch, AUTHOR, sent, calls)
    result = await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                             CONTEXT)
    assert sent['contracts/parts/frontend/browser/pages.json'] == json.dumps(PAGES)
    assert 'tests/browser/frontend/tests/support/index.ts' in sent
    assert not any('PRIVATE' in text for text in sent.values())
    assert json.loads(sent['input.json'])['routes'] == CONTEXT['routes']
    # The browser runner alone: built on the pinned image without pulling, with the page
    # contract and the API's operations.
    (args, files), = [(call[1], call[2]) for call in calls
                      if call[0] == 'build' and isinstance(call[2], dict)]
    assert f'BASE_IMAGE={PLAYWRIGHT_IMAGE}' in args and '--pull=false' in args
    # Run on its own, it names its tests after its member, and holds the suite to the
    # image's Playwright.
    assert 'CLASSNAME_PREFIX=frontend~browser:' in args
    assert f'PLAYWRIGHT_VERSION={PLAYWRIGHT_VERSION}' in args
    assert files['contract/pages.json'] == json.dumps(PAGES)
    assert json.loads(files['contract/operations.json'])[0]['operationId'] == 'getMe'
    assert files['repository/frontend/src/popup.ts'] == 'PRIVATE CLIENT SOURCE'
    assert files['suite/tests/popup.spec.ts'] == POPUP_TEST
    assert ('image', 'inspect', '--format', '{{.Id}}', PLAYWRIGHT_IMAGE) in calls
    assert result['added_tests'] == [f'frontend~browser:{case}' for case in POPUP_CASES]
    # Its runner is the one the delivery's runs start, leaving its own reports.
    assert components.read_runners(session, IMAGE, PYTHON.test_reports) == [
        {'image': IMAGE, 'member': 'frontend~browser',
         'reports': {'directory': '/tests/reports', 'pattern': '*.xml'}}]


async def test_a_browser_test_tagged_with_a_viewport_the_contract_lacks_is_refused(
        session, monkeypatch):
    fake_docker(monkeypatch, {**AUTHOR, 'browser/frontend/tests/popup.spec.ts':
                              POPUP_TEST.replace("'@narrow'", "'@tablet'")}, {}, [])
    with pytest.raises(RuntimeError, match=re.escape(
            'tagged with a viewport the page contract of part frontend does not have '
            '(desktop, narrow): frontend~browser:tests/popup.spec.ts#tablet')):
        await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                        CONTEXT)


def report(cases: list[tuple[str, str, str | None]]) -> bytes:
    body = ''.join(f'<testcase classname="{classname}" name="{name}">'
                   + (f'<failure message="{failure}"/>' if failure else '') + '</testcase>'
                   for classname, name, failure in cases)
    data = f'<testsuites><testsuite>{body}</testsuite></testsuites>'.encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        info = tarfile.TarInfo('./junit.xml')
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return stream.getvalue()


async def test_the_browser_runner_runs_after_the_in_process_one_and_one_report_is_read(
        session, tmp_path, monkeypatch):
    """The service's in-process tests and the client's browser tests: two runners, one
    after the other, whose reports are read as one run, by part, with the failing
    viewport named."""
    write(session, {
        'routing/routes.json': json.dumps({'routes': {}, 'parts': [SERVICE.record(),
                                                                   BROWSER.record()]}),
        'routing/components/runtime.json': '{}',
        'component-tests/suite/tests/test_rooms.py': 'def test_a_merged_room_is_closed(): ...\n',
        **{f'component-tests/suite/{name}': text for name, text in AUTHOR.items()},
        'component-tests/image.json': json.dumps({'image_id': IMAGE}),
    })
    browser = 'sha256:' + 'b' * 64
    components.record_runners(session, IMAGE, [
        {'image': IMAGE, 'member': None,
         'reports': {'directory': '/tests/reports', 'pattern': 'junit*.xml'}},
        {'image': browser, 'member': 'frontend~browser',
         'reports': {'directory': '/tests/reports', 'pattern': '*.xml'}}])
    # The Docker CLI's `cp` of each runner's reports: the service's first, then the
    # browser tests', told apart by the order of the copies.
    archives = tmp_path / 'archives'
    archives.mkdir()
    (archives / '0').write_bytes(report([('tests.test_rooms', 'test_a_merged_room_is_closed',
                                          None)]))
    (archives / '1').write_bytes(report([
        ('frontend~browser:tests/popup.spec.ts#desktop',
         'the requirement popup &gt; is fully visible', None),
        ('frontend~browser:tests/popup.spec.ts#narrow',
         'the requirement popup &gt; is fully visible', 'cut off')]))
    executable = tmp_path / 'bin' / 'docker'
    executable.parent.mkdir()
    executable.write_text(
        f'#!{sys.executable}\nimport pathlib, sys\n'
        f'archives = pathlib.Path({str(archives)!r})\n'
        'if sys.argv[1] != "cp":\n'
        '    sys.exit(0)\n'
        'count = archives / "count"\n'
        'index = int(count.read_text()) if count.exists() else 0\n'
        'count.write_text(str(index + 1))\n'
        'sys.stdout.buffer.write((archives / str(index)).read_bytes())\n')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(executable.parent) + os.pathsep + os.environ['PATH'])
    created: list = []

    async def docker(*args, **kwargs):
        if args[:2] == ('image', 'inspect'):
            return '10001'
        if args[0] == 'create':
            created.append(args)
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': len(created) - 1, 'OOMKilled': False})
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    tracked = [f'frontend~browser:{case}' for case in POPUP_CASES]
    result = await ActivityEnvironment().run(verification.run_component_suite, 'session',
                                             IMAGE, 'python', tracked)
    # In-process first, then the browser runner, with Chromium's room.
    assert [args[-1] for args in created] == [IMAGE, browser]
    assert '--pids-limit=256' in created[0] and '--shm-size=512m' in created[1]
    assert result['status'] == 'failed', result['error']
    assert result['exit_code'] == 1
    assert result['tracked'] == {tracked[0]: {'tests': 1, 'failed': 0},
                                 tracked[1]: {'tests': 1, 'failed': 1}}
    assert result.get('parts') == {
        'service': {'tests': 1, 'passed': 1, 'failures': 0, 'errors': 0, 'skipped': 0},
        'frontend~browser': {'tests': 2, 'passed': 1, 'failures': 1, 'errors': 0,
                             'skipped': 0}}
    failure, = result['test_failures']
    assert failure['class_name'] == 'frontend~browser:tests/popup.spec.ts#narrow'
    assert result['missing_tests'] == []


def test_runners_exit_as_the_worst_of_them():
    worst = verification.worst_exit
    assert worst([0, 0]) == 0 and worst([1, 0]) == 1 and worst([0, 1]) == 1
    assert worst([1, 2]) == 2 and worst([2, 1]) == 2
    assert worst([137, 1]) == 137 and worst([1, 124]) == 124 and worst([0, None]) is None


# ---- pulling the runner image ahead ----

def fake_pulls(monkeypatch, present: bool, failures: int = 0) -> list:
    calls: list = []
    state = {'present': present, 'failures': failures}

    async def docker(*args, **kwargs):
        calls.append(args[:2])
        if args[:2] == ('image', 'inspect'):
            if not state['present']:
                raise RuntimeError('Docker image failed: No such image')
            return 'sha256:' + 'a' * 64
        if args[0] == 'pull':
            await asyncio.sleep(0.05)
            if state['failures']:
                state['failures'] -= 1
                raise RuntimeError('Docker pull failed: registry unavailable')
            state['present'] = True
            return ''
        raise AssertionError(args)

    monkeypatch.setattr(docker_agents, 'docker', docker)
    monkeypatch.setattr(docker_agents, 'PULL_BACKOFF_SECONDS', (0, 0))
    monkeypatch.setattr(docker_agents, '_pulls', {})
    return calls


async def test_a_present_image_is_inspected_and_not_pulled(monkeypatch):
    calls = fake_pulls(monkeypatch, present=True)
    await docker_agents.ensure_image(PLAYWRIGHT_IMAGE)
    assert calls == [('image', 'inspect')]


async def test_a_missing_image_is_pulled_once_for_every_caller(monkeypatch):
    calls = fake_pulls(monkeypatch, present=False)
    # The worker's start-up pull and an activity that needs the image, at once.
    await asyncio.gather(docker_agents.ensure_images((PLAYWRIGHT_IMAGE,)),
                         docker_agents.ensure_image(PLAYWRIGHT_IMAGE))
    assert calls.count(('pull', '--quiet')) == 1
    await docker_agents.ensure_image(PLAYWRIGHT_IMAGE)
    assert calls.count(('pull', '--quiet')) == 1


async def test_a_failed_pull_is_retried_then_names_the_image(monkeypatch):
    calls = fake_pulls(monkeypatch, present=False, failures=2)
    await docker_agents.ensure_image(PLAYWRIGHT_IMAGE)
    assert calls.count(('pull', '--quiet')) == 3
    calls = fake_pulls(monkeypatch, present=False, failures=3)
    with pytest.raises(docker_agents.ImageUnavailable, match=re.escape(PLAYWRIGHT_IMAGE)):
        await docker_agents.ensure_image(PLAYWRIGHT_IMAGE)
    assert calls.count(('pull', '--quiet')) == 3
    # Not a mismatch of a contract or a failed test, which are RuntimeErrors.
    assert not issubclass(docker_agents.ImageUnavailable, RuntimeError)
    # At the worker's start a failure is logged, not raised.
    fake_pulls(monkeypatch, present=False, failures=3)
    await docker_agents.ensure_images((PLAYWRIGHT_IMAGE,))


async def test_the_worker_starts_without_waiting_for_the_pull(monkeypatch):
    from app.agent_workflows import worker
    pulling = asyncio.Event()
    started: list = []

    async def ensure_images(references):
        pulling.set()
        await asyncio.sleep(3600)

    async def ensure_schema():
        started.append('schema')

    class Client:
        @staticmethod
        async def connect(address):
            return object()

    async def run_worker(*workers):
        await pulling.wait()
        started.append(len(workers))

    monkeypatch.setattr(worker.docker_agents, 'ensure_images', ensure_images)
    monkeypatch.setattr(worker, 'ensure_settings_schema', ensure_schema)
    monkeypatch.setattr(worker, 'Client', Client)
    monkeypatch.setattr(worker, 'Worker', lambda *args, **kwargs: object())
    monkeypatch.setattr(worker, 'run_worker', run_worker)
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    await asyncio.wait_for(worker.main(), 5)
    assert started == ['schema', len(worker.STAGES)]


# ---- the routing's page contract ----

async def test_the_routing_keeps_the_checked_page_contract_with_the_apis_operations(
        tmp_path, monkeypatch):
    session_dir = tmp_path / 'session'
    write(session_dir, {'repository/Dockerfile': 'FROM python:3.12-slim',
                        'repository/frontend/src/popup.ts': 'CLIENT SOURCE'})
    output = tmp_path / 'output'
    write(output / 'parts/frontend/browser', contract_files())
    checks: list = []

    async def check_page_conformance(contract, repository, project, browser, operations,
                                     absent, work, log_dir):
        checks.append((project, sorted(path.name for path in browser.iterdir()),
                       [item['operationId'] for item in operations], absent,
                       (repository / 'frontend/src/popup.ts').read_text()))

    monkeypatch.setattr(routing, 'check_page_conformance', check_page_conformance)
    routes = {'FR-171': {'route': 'browser', 'reason': 'Cut off.',
                         'page': {**PAGE, 'roles': [*PAGE['roles'], {'testid': 'popup'}],
                                  'added': [{'testid': 'popup'}]}}}
    artifact_dir = session_dir / 'routing'
    work = tmp_path / 'work'
    work.mkdir()
    routed = await routing.publish_components(PYTHON, session_dir, output, routes, work,
                                              artifact_dir, [SERVICE, FRONTEND, BROWSER],
                                              DOCUMENT)
    assert routed == ['frontend~browser']
    # Checked against the code the delivery started from, the added element absent.
    assert checks == [('frontend', ['fixtures', 'pages.json', 'viewports.json'],
                       ['getMe', 'getRoom'], [{'testid': 'popup'}], 'CLIENT SOURCE')]
    assert json.loads((artifact_dir / 'parts/frontend/browser/pages.json').read_text()) == PAGES
    assert json.loads(BROWSER.operations_file(session_dir).read_text()) == operations_list(
        DOCUMENT)

    # Without an API contract, a page that calls no operation is still routed to.
    shutil.rmtree(output / 'parts/frontend/browser')
    write(output / 'parts/frontend/browser', contract_files(
        pages=_pages(operations=[], actions=[]), fixtures={}))
    work = tmp_path / 'work-0'
    work.mkdir()
    assert await routing.publish_components(
        PYTHON, session_dir, output, {'FR-171': {'route': 'browser', 'reason': 'Cut off.',
                                                 'page': {**PAGE, 'roles': PAGE['roles'][:1]}}},
        work, artifact_dir, [SERVICE, FRONTEND, BROWSER], None) == ['frontend~browser']
    assert json.loads(BROWSER.operations_file(session_dir).read_text()) == []
    write(output / 'parts/frontend/browser', contract_files())
    work = tmp_path / 'work-1'
    work.mkdir()
    with pytest.raises(RuntimeError, match='operations the API does not have: getMe, getRoom'):
        await routing.publish_components(PYTHON, session_dir, output, routes, work,
                                         artifact_dir, [SERVICE, FRONTEND, BROWSER], None)

    # An unusable contract names its part.
    write(output / 'parts/frontend/browser', contract_files(viewports={
        **VIEWPORTS, 'tablet': {'width': 768, 'height': 1024, 'input': 'touch'}}))
    work = tmp_path / 'work-2'
    work.mkdir()
    with pytest.raises(RuntimeError, match='The page contract of part frontend is unusable: No '
                                           'route names the viewports tablet'):
        await routing.publish_components(PYTHON, session_dir, output, routes, work,
                                         artifact_dir, [SERVICE, FRONTEND, BROWSER], DOCUMENT)


def test_a_malformed_api_contract_is_the_routings_own_error(tmp_path):
    from temporalio.exceptions import ApplicationError
    checkout = tmp_path / 'session/repository'
    write(checkout, {'contracts/openapi.yaml': 'paths: [unclosed',
                     'frontend/index.html': ''})
    context = {'brief': {'requirements': [{'id': 'FR-171'}]}}
    with pytest.raises(ApplicationError) as raised:
        routing.prepare_routing(PYTHON, tmp_path / 'session', context, tmp_path / 'workspace',
                                [SERVICE, FRONTEND, BROWSER])
    assert raised.value.type == 'RoutingError' and raised.value.non_retryable
    assert "OpenAPI document cannot be read" in raised.value.message


async def test_the_runners_of_a_run_share_its_bound(session, monkeypatch):
    """A run whose browser runner would outlast the activity is stopped there and reported
    as stopped, not left to the workflow's timeout."""
    write(session, {'component-tests/image.json': json.dumps({'image_id': IMAGE}),
                    **{f'component-tests/suite/{name}': text for name, text in AUTHOR.items()}})
    browser = 'sha256:' + 'b' * 64
    components.record_runners(session, IMAGE, [
        {'image': IMAGE, 'member': 'frontend~browser',
         'reports': {'directory': '/tests/reports', 'pattern': '*.xml'}},
        {'image': browser, 'member': 'frontend~browser',
         'reports': {'directory': '/tests/reports', 'pattern': '*.xml'}}])
    waits: list = []
    now = [0.0]

    async def run_runner(session_id, image, is_browser, reports, destination, log_file,
                         seconds=0):
        waits.append((image, seconds))
        now[0] += 700
        return {'exit_code': 0, 'error': None, 'log': '', 'oom_killed': False}

    async def ensure_image(reference):
        return None

    monkeypatch.setattr(verification, '_run_runner', run_runner)
    monkeypatch.setattr(docker_agents, 'ensure_image', ensure_image)
    monkeypatch.setattr(verification, 'RUN_SECONDS', 800)
    monkeypatch.setattr(verification.time, 'monotonic', lambda: now[0])
    result = await ActivityEnvironment().run(verification.run_component_suite, 'session',
                                             IMAGE, 'python', [])
    # The first runner gets its own bound; no time is left to start the second.
    assert waits == [(IMAGE, 750)]
    assert result['exit_code'] == 124 and result['status'] == 'error'
    assert 'timed out or were terminated' in (result['error'] or '')
