import shutil
import subprocess
import sys
from xml.etree import ElementTree

from app.languages import get_language

SUITE = get_language('python').test_suite
TESTS = '''import pytest

from base import Base


def test_function():
    pass


@pytest.mark.parametrize("value", [1, 2])
def test_parameterized(value):
    pass


class TestOuter:
    def test_method(self):
        pass

    @pytest.mark.parametrize("value", [1, 2])
    def test_parameterized_in_class(self, value):
        pass

    class TestInner:
        async def test_nested(self):
            pass

    def helper(self):
        pass


class TestChild(TestOuter):
    def test_method(self):
        pass


class TestImported(Base):
    pass
'''
# Bases from the suite's own modules, as a test module imports them.
MODULES = {
    'base.py': 'from shared import Root\n\n\nclass Base(Root):\n    def test_base(self):\n        pass\n',
    'shared.py': 'class Root:\n    def test_root(self):\n        pass\n',
}


def project(tmp_path, tests=TESTS):
    root = tmp_path / 'suite'
    shutil.copytree(SUITE.template, root)
    (root / 'tests/api').mkdir(parents=True)
    (root / 'tests/api/test_cases.py').write_text(tests)
    for name, source in MODULES.items():
        (root / 'tests/api' / name).write_text(source)
    return root


def test_test_identities_are_those_pytest_reports(tmp_path):
    root = project(tmp_path)
    report = tmp_path / 'junit.xml'
    subprocess.run([sys.executable, '-m', 'pytest', f'--junitxml={report}', '-p', 'no:asyncio',
                    '-W', 'ignore'], cwd=root, capture_output=True, check=False)
    reported = {SUITE.case_id(case.get('classname'), case.get('name'))
                for case in ElementTree.parse(report).getroot().iter('testcase')}
    assert reported == set(SUITE.test_cases(root)) == {
        'tests.api.test_cases#test_function', 'tests.api.test_cases#test_parameterized',
        'tests.api.test_cases.TestOuter#test_method',
        'tests.api.test_cases.TestOuter#test_parameterized_in_class',
        'tests.api.test_cases.TestOuter.TestInner#test_nested',
        'tests.api.test_cases.TestChild#test_method',
        'tests.api.test_cases.TestChild#test_parameterized_in_class',
        'tests.api.test_cases.TestChild.TestInner#test_nested',
        'tests.api.test_cases.TestImported#test_base',
        'tests.api.test_cases.TestImported#test_root'}


def test_a_test_changes_with_what_decides_how_it_runs_not_with_its_layout(tmp_path):
    original = SUITE.test_cases(project(tmp_path / 'original'))
    reformatted = SUITE.test_cases(project(tmp_path / 'reformatted', TESTS.replace(
        'def test_function():\n    pass', 'def test_function(\n):\n        pass')))
    assert reformatted == original
    skipped = SUITE.test_cases(project(tmp_path / 'skipped', TESTS.replace(
        'class TestOuter:', '@pytest.mark.skip\nclass TestOuter:')))
    # pytest applies a class's marks to its subclasses, even to the tests they override.
    assert {case for case in original if skipped[case] != original[case]} == {
        'tests.api.test_cases.TestOuter#test_method',
        'tests.api.test_cases.TestOuter#test_parameterized_in_class',
        'tests.api.test_cases.TestOuter.TestInner#test_nested',
        'tests.api.test_cases.TestChild#test_method',
        'tests.api.test_cases.TestChild#test_parameterized_in_class',
        'tests.api.test_cases.TestChild.TestInner#test_nested'}
