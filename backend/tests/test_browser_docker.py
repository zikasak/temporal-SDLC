"""Opt-in check of the browser route's runners with real Docker: the page contract's
conformance check and the browser tests' runner, on the pinned Playwright image, against
a small Vite page (tests/browser_page) whose requirement popup is drawn inside a scrolling
panel, portaled to the body, or inside the panel below 600 px only.

Run: RUN_DOCKER_VERIFICATION_TESTS=1 python -m pytest tests/test_browser_docker.py -q -n0
Requires Docker and outbound access to pull the Playwright image and npm packages.
"""
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from xml.etree import ElementTree

import pytest

from app import docker_agents
from app.agent_workflows.host import build_image, copy_regular_files
from app.images import PLAYWRIGHT_IMAGE, PLAYWRIGHT_VERSION
from app.languages import get_language, get_part_language
from app.languages.typescript.browser import BROWSER_CONTRACT, BROWSER_SUITE
from app.languages.composite import CompositeSuite
from app.routing.parts import ComponentPart, component_suite
from app.routing.browser import check_page_conformance, operations_list, read_document
from app.test_generation.components import runner_context

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_DOCKER_VERIFICATION_TESTS') != '1', reason='real Docker tests are opt-in',
)

PAGE = Path(__file__).parent / 'browser_page'
PROJECT = 'page'
# The delivery's suite the runner's tests are a member of, as `page~browser`.
_suite = component_suite(get_language('python'), [
    ComponentPart(PROJECT, PROJECT, get_part_language('typescript'), browser=True)])
assert isinstance(_suite, CompositeSuite)
SUITE = _suite
MEMBER = 'page~browser:'
VIEWPORTS = {'desktop': {'width': 1280, 'height': 800, 'input': 'mouse'},
             'narrow': {'width': 390, 'height': 844, 'input': 'touch'}}
PAGES = {'api_prefix': '/api', 'pages': [
    {'entry': f'/?popup={variant}', 'operations': ['listItems'],
     'elements': [{'role': 'region', 'name': 'Answer'}, {'role': 'button', 'name': 'FR-1'}],
     'actions': [{'target': {'role': 'button', 'name': 'FR-1'}, 'input': 'reveal',
                  'shows': [{'role': 'tooltip'}]}]}
    for variant in ('inside', 'portaled', 'narrow-bug')]}

POPUP = '''import {{ test, expect, fixture, reveal, settle, expectFullyVisible }} from './support';

test.describe('the requirement popup', () => {{
  test('drawn {variant} is fully visible', {{ tag: [{tags}] }}, async ({{ page }}) => {{
    await fixture(page, 'listItems');
    await page.goto('/?popup={variant}');
    await expect(page.getByText('a, b')).toBeVisible();
    await reveal(page.getByRole('button', {{ name: 'FR-1', exact: true }}));
    await settle(page);
    await expectFullyVisible(page.getByRole('tooltip'));
  }});
}});
'''


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / 'repository'
    copy_regular_files(PAGE, repository / PROJECT,
                       include=lambda relative: relative.parts[0] != 'node_modules')
    return repository


def _contract(tmp_path: Path, viewports: dict | None = None) -> Path:
    contract = tmp_path / 'contract'
    (contract / 'fixtures').mkdir(parents=True)
    (contract / 'pages.json').write_text(json.dumps(PAGES))
    (contract / 'viewports.json').write_text(json.dumps(viewports or VIEWPORTS))
    (contract / 'fixtures/listItems.json').write_text(
        json.dumps({'status': 200, 'body': {'items': ['a', 'b']}}))
    # Runner fixtures beside them, not checked by the host here: JSON null for a JSON
    # response, and the response without content.
    (contract / 'fixtures/listItems.null.json').write_text(
        json.dumps({'status': 200, 'body': None}))
    (contract / 'fixtures/listItems.unavailable.json').write_text(
        json.dumps({'status': 503, 'body': None}))
    return contract


def _operations() -> list:
    return operations_list(read_document((PAGE / 'contracts/openapi.yaml').read_text()))


async def _run(tmp_path: Path, tests: dict[str, str], viewports: dict | None = None,
               package: Path | None = None) -> tuple[int, dict[str, str | None]]:
    """Build the browser runner of a suite of `tests` against the page and run it without
    a network: its exit status and each reported test's failure message (None when it
    passed), by identity."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    suite = tmp_path / 'suite'
    shutil.copytree(BROWSER_SUITE.template, suite,
                    ignore=shutil.ignore_patterns(*BROWSER_SUITE.runner))
    for name, text in tests.items():
        (suite / 'tests' / name).write_text(text)
    if package is not None:
        # Another project's manifest and lock file.
        for name in ('package.json', 'package-lock.json'):
            shutil.copyfile(package / name, suite / name)
    context = tmp_path / 'context'
    await docker_agents.ensure_image(PLAYWRIGHT_IMAGE)
    runner_context(BROWSER_SUITE, _repository(tmp_path), suite, context)
    shutil.copytree(_contract(tmp_path, viewports), context / 'contract')
    (context / 'contract/operations.json').write_text(json.dumps(_operations()))
    # As build_runner builds it: named after its member, held to the image's Playwright.
    image = await build_image(context, 'browser test runner', tmp_path / 'build.log',
                              build_args={**BROWSER_SUITE.build_args({'project': PROJECT}),
                                          'CLASSNAME_PREFIX': MEMBER}, pull=False)
    name = f'browser-test-{uuid.uuid4().hex}'
    try:
        await docker_agents.docker(
            'create', '--name', name, '--network=none', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--pids-limit=1024', '--memory=4g',
            '--shm-size=512m', image, timeout=60)
        await docker_agents.docker('start', name, timeout=60)
        status = int(await docker_agents.docker('wait', name, timeout=700))
        await docker_agents.docker('cp', f'{name}:/tests/reports/junit.xml',
                                   str(tmp_path / 'junit.xml'), timeout=60)
    finally:
        await docker_agents.docker('rm', '-f', name, timeout=60)
        await docker_agents.docker('image', 'rm', '-f', image, timeout=60)
    cases: dict[str, str | None] = {}
    for case in ElementTree.parse(tmp_path / 'junit.xml').getroot().iter('testcase'):
        failure = case.find('failure')
        # As a delivery reads the report: by the member its class name starts with.
        assert SUITE.part_of(case.get('classname', '')) == 'page~browser'
        identity = SUITE.case_id(case.get('classname', ''), case.get('name', ''))
        assert identity.startswith(MEMBER), identity
        cases[identity.removeprefix(MEMBER)] = (None if failure is None
                                                else failure.get('message', ''))
    assert set(cases) <= set(BROWSER_SUITE.test_cases(suite)), 'the host names the tests as reported'
    return status, cases


async def test_a_popup_cut_off_by_its_panel_fails_where_a_portaled_one_passes(tmp_path):
    status, cases = await _run(tmp_path, {
        'inside.spec.ts': POPUP.format(variant='inside', tags="'@desktop', '@narrow'"),
        'portaled.spec.ts': POPUP.format(variant='portaled', tags="'@desktop', '@narrow'"),
    })
    assert status == 1
    for viewport in ('desktop', 'narrow'):
        inside = cases[f'tests/inside.spec.ts#{viewport} > the requirement popup > '
                       'drawn inside is fully visible']
        assert inside and ('is cut off or covered' in inside
                           or 'is not inside the window' in inside), viewport
        assert cases[f'tests/portaled.spec.ts#{viewport} > the requirement popup > '
                     'drawn portaled is fully visible'] is None


async def test_viewports_run_only_the_tests_tagged_with_them(tmp_path):
    status, cases = await _run(tmp_path, {
        'narrow.spec.ts': POPUP.format(variant='narrow-bug', tags="'@narrow'"),
        'desktop.spec.ts': POPUP.format(variant='narrow-bug', tags="'@desktop'"),
    })
    assert status == 1
    assert cases == {
        'tests/narrow.spec.ts#narrow > the requirement popup > drawn narrow-bug is fully '
        'visible': cases['tests/narrow.spec.ts#narrow > the requirement popup > drawn '
                         'narrow-bug is fully visible'],
        'tests/desktop.spec.ts#desktop > the requirement popup > drawn narrow-bug is fully '
        'visible': None,
    }
    assert cases['tests/narrow.spec.ts#narrow > the requirement popup > drawn narrow-bug is '
                 'fully visible']
    # A contract with the narrow viewport alone runs no desktop project.
    status, cases = await _run(tmp_path / 'only-narrow', {
        'narrow.spec.ts': POPUP.format(variant='portaled', tags="'@narrow'"),
    }, viewports={'narrow': VIEWPORTS['narrow']})
    assert status == 0
    assert list(cases) == ['tests/narrow.spec.ts#narrow > the requirement popup > drawn '
                           'portaled is fully visible']


async def test_a_request_the_test_did_not_answer_fails_it_and_nothing_else_is_reachable(
        tmp_path):
    status, cases = await _run(tmp_path, {'requests.spec.ts': '''
import { test, expect, fixture } from './support';

test('an unanswered operation fails the test', { tag: '@desktop' }, async ({ page }) => {
  await page.goto('/?popup=portaled');
  await expect(page.getByText('unavailable')).toBeVisible();
});

test('the network is not reachable', { tag: '@desktop' }, async ({ page }) => {
  await fixture(page, 'listItems');
  await page.goto('/?popup=portaled');
  const outside = await fetch('http://1.1.1.1/').then(() => 'reached', () => 'unreachable');
  expect(outside).toBe('unreachable');
});

test('a null body is JSON null', { tag: '@desktop' }, async ({ page }) => {
  await fixture(page, 'listItems', 'null');
  await page.goto('/?popup=portaled');
  expect(await page.evaluate(() => fetch('/api/items').then((response) => response.json())))
    .toBeNull();
  await fixture(page, 'listItems', 'unavailable');
  expect(await page.evaluate(() => fetch('/api/items').then(
    (response) => response.text().then((text) => [response.status, text]))))
    .toEqual([503, '']);
});
'''})
    assert status == 1
    failure = cases['tests/requests.spec.ts#desktop > an unanswered operation fails the test']
    assert failure and 'did not answer with a fixture: listItems' in failure
    assert cases['tests/requests.spec.ts#desktop > the network is not reachable'] is None
    assert cases['tests/requests.spec.ts#desktop > a null body is JSON null'] is None


@pytest.mark.parametrize('change, absent, error', [
    (None, [], None),
    # An element the change is to add that its page has already.
    (None, [{'entry': '/?popup=inside', 'element': {'role': 'region', 'name': 'Answer'}}],
     'Page /\\?popup=inside at desktop: role=region\\[name="Answer"\\] is present already'),
    # Added to one page, it may be on the others; one page lacking it is checked alone.
    (lambda pages: pages['pages'].append({'entry': '/?popup=none', 'elements': [
        {'role': 'region', 'name': 'Answer'}, {'testid': 'added'}], 'operations': ['listItems']}),
     [{'entry': '/?popup=none', 'element': {'testid': 'added'}}], None),
    # A declared element the page does not have.
    (lambda pages: pages['pages'][0]['elements'].append({'testid': 'missing'}), [],
     'testid=missing is not present'),
    # An operation the page calls that the contract does not declare.
    (lambda pages: pages['pages'][0].update(operations=[]), [],
     'calls listItems, which the page contract does not declare'),
])
async def test_the_page_contract_conforms_to_the_built_page_and_nothing_else(
        tmp_path, change, absent, error):
    contract = _contract(tmp_path)
    pages = json.loads((contract / 'pages.json').read_text())
    if change:
        change(pages)
    (contract / 'pages.json').write_text(json.dumps(pages))
    work = tmp_path / 'work'
    work.mkdir()
    check = check_page_conformance(BROWSER_CONTRACT, _repository(tmp_path), PROJECT, contract,
                                   _operations(), absent, work, tmp_path / 'logs')
    if error is None:
        await check
    else:
        with pytest.raises(RuntimeError, match=error):
            await check


async def test_a_suite_that_installs_another_playwright_does_not_build(tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    package = json.loads((BROWSER_SUITE.template / 'package.json').read_text())
    package['devDependencies']['@playwright/test'] = '1.60.0'
    (other / 'package.json').write_text(json.dumps(package))
    subprocess.run(['npm', 'install', '--package-lock-only', '--ignore-scripts', '--no-audit',
                    '--no-fund'], cwd=other, check=True, capture_output=True)
    with pytest.raises(RuntimeError, match=f'keep @playwright/test at {PLAYWRIGHT_VERSION}'):
        await _run(tmp_path / 'run', {'popup.spec.ts': POPUP.format(variant='portaled',
                                                                    tags="'@desktop'")},
                   package=other)
