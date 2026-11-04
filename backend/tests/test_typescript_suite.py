"""Component suites of TypeScript parts: the vitest template and its trusted runner files,
and the identity of the tests, read on the host as vitest's JUnit report names them."""
import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from app.languages import get_part_language
from app.test_generation import activities as contract_tests
from tests.test_typescript_components import FRONTEND

SUITE = get_part_language('typescript').component_suite

# TR-6 of the flow-closing bugfix, at the seam plans/part-component-contracts.md routes it
# to: the GitHub App manifest the web client posts subscribes to pull_request events.
MANIFEST_TEST = '''import { afterEach, describe, expect, it, vi } from 'vitest';
import { createGitHubApp } from '@part/src/github';

describe('the GitHub App manifest', () => {
  afterEach(() => vi.restoreAllMocks());

  it('subscribes the app to pull_request events', () => {
    let manifest: { default_events: string[] } | undefined;
    vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(function (this: HTMLFormElement) {
      const input = this.querySelector('input[name="manifest"]') as HTMLInputElement;
      manifest = JSON.parse(input.value);
    });
    createGitHubApp('flows', '');
    expect(manifest?.default_events).toContain('pull_request');
  });
});
'''
# Regular expressions and JSX, which hold what would be quotes and brackets in code.
ELEMENT_TEST = '''import { describe, expect, it, test } from 'vitest';

const pattern = /it's (a|b)\\/[)}]/g;

describe("flows \\u00e9", () => {
  describe.skip(`closed`, () => {
    test.runIf(true)('it\\'s <p> ok', () => {
      const ratio = 1 / 2 / 3;
      expect(<p className="a">it's {ratio > 1 ? <b>y</b> : "z"}</p>).toBeTruthy();
      expect(pattern.test("x")).toBe(true);
    });
  });
  it.todo('later');
});
'''


def write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)


def test_the_typescript_component_suite_template_publishes_no_runner(tmp_path):
    assert all((SUITE.template / name).is_file() for name in (*SUITE.runner, *SUITE.required))
    contract_tests.copy_test_suite(SUITE, SUITE.template, tmp_path / 'suite')
    assert sorted(item.name for item in (tmp_path / 'suite').iterdir()) == [
        'README.md', 'package-lock.json', 'package.json', 'tsconfig.json']
    # The template's lock file installs exactly what its package.json pins.
    manifest = json.loads((SUITE.template / 'package.json').read_text())
    lock = json.loads((SUITE.template / 'package-lock.json').read_text())
    assert lock['packages']['']['devDependencies'] == manifest['devDependencies']
    assert {'vitest', 'jsdom'} <= set(manifest['devDependencies'])


def test_tests_are_named_by_their_file_and_the_describe_blocks_around_them(tmp_path):
    write(tmp_path, {'tests/manifest.test.ts': MANIFEST_TEST,
                     'tests/flows/closed.test.tsx': ELEMENT_TEST,
                     # Not test files.
                     'tests/helpers.ts': "test('helper', () => {});",
                     'tests/notes.test.md': "test('notes', () => {});"})
    cases = SUITE.test_cases(tmp_path)
    assert sorted(cases) == [
        'tests/flows/closed.test.tsx#flows é > closed > it\'s <p> ok',
        'tests/flows/closed.test.tsx#flows é > later',
        'tests/manifest.test.ts#the GitHub App manifest > subscribes the app to pull_request '
        'events']
    assert SUITE.untracked_tests(tmp_path) == []
    # As vitest's JUnit report names them.
    assert SUITE.case_id('tests/flows/closed.test.tsx', 'flows é > later') in cases


def test_a_test_is_changed_by_its_code_or_the_describe_calls_around_it_not_its_layout(
        tmp_path):
    write(tmp_path, {'tests/manifest.test.ts': MANIFEST_TEST,
                     'tests/flows/closed.test.tsx': ELEMENT_TEST})
    before = SUITE.test_cases(tmp_path)
    write(tmp_path, {'tests/manifest.test.ts': MANIFEST_TEST.replace(
        "    createGitHubApp('flows', '');",
        "    // The organization is optional.\n    createGitHubApp(\n      'flows',\n      ''\n    );"),
        'tests/flows/closed.test.tsx': ELEMENT_TEST.replace(
            '<p className="a">', '<p\n        className="a">')})
    assert SUITE.test_cases(tmp_path) == before
    write(tmp_path, {'tests/flows/closed.test.tsx': ELEMENT_TEST.replace(
        'describe.skip(`closed`', 'describe(`closed`')})
    after = SUITE.test_cases(tmp_path)
    assert [case for case in after if after[case] != before[case]] == [
        'tests/flows/closed.test.tsx#flows é > closed > it\'s <p> ok']


def test_a_test_that_could_not_be_tracked_rejects_the_suite(tmp_path):
    write(tmp_path, {**{name: (SUITE.template / name).read_text() for name in SUITE.required},
                     'tests/manifest.test.ts': MANIFEST_TEST,
                     'tests/flows.test.ts': '''import { describe, it, test } from 'vitest';
describe('flows', () => {
  test.each([1, 2])('case %s', (n) => {});
  it(name, () => {});
  it('a' + suffix, () => {});
  it(`b ${suffix}`, () => {});
});
describe(Flows, () => { test('inside an untracked block', () => {}); });
''',
                     'tests/broken.test.tsx': "test('a', () => <p>unclosed);\n"})
    assert SUITE.untracked_tests(tmp_path) == [
        'tests/broken.test.tsx cannot be read: line 1: element p is not closed',
        'tests/flows.test.ts line 3: a parameterized test (test.each) cannot be tracked; '
        'write one test per case',
        *[f'tests/flows.test.ts line {line}: a test named by an expression cannot be '
          'tracked; name it with a string literal' for line in (4, 5, 6)],
        'tests/flows.test.ts line 8: a describe block named by an expression cannot be '
        'tracked; name it with a string literal']
    assert list(SUITE.test_cases(tmp_path)) == [
        'tests/manifest.test.ts#the GitHub App manifest > subscribes the app to pull_request '
        'events']
    with pytest.raises(RuntimeError, match='cannot be tracked: tests/broken.test.tsx'):
        contract_tests.validate_output(SUITE, None, False, tmp_path)


# ---- the runner's vitest, against this repository's web client ----

@pytest.fixture(scope='module')
def installed(tmp_path_factory):
    """The template installed, and a copy of this repository's web client installed from
    its lock file, as the runner installs them. Opt-in: it needs Node.js and the npm
    registry."""
    if os.getenv('RUN_NODE_CONTRACT_TESTS') != '1' or shutil.which('npm') is None:
        pytest.skip('set RUN_NODE_CONTRACT_TESTS=1 with Node.js and npm to run vitest')
    root = tmp_path_factory.mktemp('runner')
    shutil.copytree(FRONTEND, root / 'frontend',
                    ignore=shutil.ignore_patterns('node_modules', 'dist'))
    contract_tests.copy_test_suite(SUITE, SUITE.template, root / 'suite')
    for project in (root / 'frontend', root / 'suite'):
        subprocess.run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'],
                       cwd=project, check=True, capture_output=True)
    return root


def vitest(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['node_modules/.bin/vitest', *arguments,
         '--config', str(SUITE.template / 'vitest.config.mjs')],
        cwd=root / 'suite', capture_output=True, text=True, timeout=300,
        env={**os.environ, 'PART': str(root / 'frontend'), 'NO_COLOR': '1',
             'REPORT': str(root / 'suite/reports/junit.xml')})


def reported(report: Path) -> dict[str, bool]:
    """Each test case of a JUnit report by identity, and whether it passed."""
    return {SUITE.case_id(case.get('classname', ''), case.get('name', '')):
            case.find('failure') is None and case.find('error') is None
            for case in ElementTree.parse(report).iter('testcase')}


def test_the_runner_proves_tr_6_red_against_the_web_client_and_green_with_the_fix(installed):
    write(installed / 'suite', {'tests/manifest.test.ts': MANIFEST_TEST,
                                'tests/flows/closed.test.tsx': ELEMENT_TEST})
    cases = SUITE.test_cases(installed / 'suite')
    listed = vitest(installed, 'list', '--json')
    assert listed.returncode == 0, listed.stderr
    # The tokenizer names every test as vitest collects it; vitest lists no skipped test.
    assert {SUITE.case_id(Path(item['file']).relative_to(installed / 'suite').as_posix(),
                          item['name'])
            for item in json.loads(listed.stdout)} == {
        'tests/manifest.test.ts#the GitHub App manifest > subscribes the app to pull_request '
        'events'}

    run = vitest(installed, 'run')
    assert run.returncode == 1, run.stderr
    manifest = ('tests/manifest.test.ts#the GitHub App manifest > subscribes the app to '
                'pull_request events')
    # The report names every test, skipped ones too, by the identity the tokenizer gives.
    results = reported(installed / 'suite/reports/junit.xml')
    assert set(results) == set(cases) and results[manifest] is False

    github = installed / 'frontend/src/github.ts'
    original = github.read_text()
    events = '["issue_comment", "pull_request_review_comment"]'
    assert events in original
    github.write_text(original.replace(
        events, '["issue_comment", "pull_request_review_comment", "pull_request"]'))
    try:
        run = vitest(installed, 'run')
    finally:
        github.write_text(original)
    assert run.returncode == 0, run.stdout
    assert reported(installed / 'suite/reports/junit.xml')[manifest] is True


def test_a_test_importing_what_the_part_does_not_have_fails_to_collect(installed):
    write(installed / 'suite', {'tests/missing.test.ts': '''import { test } from 'vitest';
import { reopen } from '@part/src/missing';

test('reopens', () => reopen());
'''})
    try:
        listed = vitest(installed, 'list')
    finally:
        (installed / 'suite/tests/missing.test.ts').unlink()
    assert listed.returncode != 0
