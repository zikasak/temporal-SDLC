"""Opt-in check of the component contracts' conformance runners and the Java and
TypeScript component test runners with real Docker.

Run: RUN_DOCKER_VERIFICATION_TESTS=1 python -m pytest tests/test_component_contract_docker.py -q
Requires Docker and outbound access to fetch the base images, mypy, Maven artifacts and npm
packages.
"""
import json
import os
import re
import shutil
import uuid
from pathlib import Path

import pytest

from app import docker_agents
from app.agent_workflows.host import build_image
from app.languages import get_language, get_part_language
from app.routing.components import check_conformance
from app.test_generation.activities import copy_test_suite
from app.test_generation.components import runner_context
from tests.test_typescript_components import GITHUB_STUB

CONTRACT = get_language('python').component_contract

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_DOCKER_VERIFICATION_TESTS') != '1', reason='real Docker tests are opt-in',
)

SERVICE = '''from typing import Literal

import yaml

State = Literal["open", "closed"]


class Flow:
    def close(self, merged: bool) -> None:
        self.state: State = "closed"

    def fork(self, session: str) -> str:
        return yaml.safe_dump(session)
'''
# Only part of the class: the seam, not everything the module has.
STUB = '''from typing import Literal

State = Literal["open", "closed"]


class Flow:
    def close(self, merged: bool) -> None:
        """Closes the flow whether its pull request was merged or not."""
        ...
'''


@pytest.mark.parametrize('stub, error', [
    (STUB, None),
    # A seam the code does not have, and a signature the code does not have.
    (STUB + '\ndef reopen() -> None: ...\n', 'reopen is not present at runtime'),
    (STUB.replace('merged: bool', 'merged: bool, quietly: bool'), 'Flow.close is inconsistent'),
    # stubtest cannot compare a Literal alias; the runner does.
    (STUB.replace('"closed"]', '"closed", "merged"]'), 'State is inconsistent'),
])
async def test_stubs_conform_to_the_code_they_describe_and_nothing_else(tmp_path, stub, error):
    repository = tmp_path / 'repository'
    (repository / 'src/service').mkdir(parents=True)
    (repository / 'src/service/flows.py').write_text(SERVICE)
    # The module imports a dependency only its requirements file installs.
    (repository / 'requirements.txt').write_text('pyyaml\n')
    components = tmp_path / 'components'
    (components / 'service').mkdir(parents=True)
    (components / 'runtime.json').write_text(json.dumps(
        {'source_root': 'src', 'requirements': ['requirements.txt']}))
    (components / 'service/flows.pyi').write_text(stub)
    work = tmp_path / 'work'
    work.mkdir()
    check = check_conformance(CONTRACT, repository, components, ['service.flows'], work,
                              tmp_path / 'logs')
    if error is None:
        await check
    else:
        with pytest.raises(RuntimeError, match=error):
            await check


JAVA = get_language('java')
JAVA_SERVICE = '''package com.example.flows;

public class Flow {
    private String state = "open";

    public void close(boolean merged) {
        // The bug: a closed pull request leaves the flow open.
    }

    public String state() {
        return state;
    }
}
'''
JAVA_STUB = '''package com.example.flows;

/** A delivery flow. */
public class Flow {
    public Flow();

    /** The flow's pull request was closed, merged or not: the flow completes. */
    public void close(boolean merged);

    /** "open" until the flow completes, then "completed". */
    public String state();
}
'''
JAVA_TEST = '''package com.example.flows;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class FlowTest {
    @Test
    void aClosedPullRequestCompletesTheFlow() {
        Flow flow = new Flow();
        flow.close(true);
        assertEquals("completed", flow.state());
    }
}
'''
POM = '''<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.example</groupId>
    <artifactId>flows</artifactId>
    <version>1.0.0</version>
    <properties>
        <maven.compiler.release>21</maven.compiler.release>
    </properties>
</project>
'''


def java_contract(tmp_path, stub: str):
    repository = tmp_path / 'repository'
    (repository / 'service/src/main/java/com/example/flows').mkdir(parents=True)
    (repository / 'service/pom.xml').write_text(POM)
    (repository / 'service/src/main/java/com/example/flows/Flow.java').write_text(JAVA_SERVICE)
    components = tmp_path / 'components'
    (components / 'com/example/flows').mkdir(parents=True)
    (components / 'runtime.json').write_text(json.dumps({'project': 'service'}))
    (components / 'com/example/flows/Flow.java').write_text(stub)
    return repository, components


@pytest.mark.parametrize('stub, error', [
    (JAVA_STUB, None),
    (JAVA_STUB.replace('public String state();', 'public String state();\n\n    public void reopen();'),
     'com.example.flows.Flow.reopen() is not present at runtime'),
    (JAVA_STUB.replace('boolean merged', 'String merged'),
     'com.example.flows.Flow.close(java.lang.String) is not present at runtime'),
])
async def test_java_stubs_conform_to_the_compiled_code_they_describe(tmp_path, stub, error):
    repository, components = java_contract(tmp_path, stub)
    work = tmp_path / 'work'
    work.mkdir()
    check = check_conformance(JAVA.component_contract, repository, components,
                              ['com.example.flows.Flow'], work, tmp_path / 'logs')
    if error is None:
        await check
    else:
        with pytest.raises(RuntimeError, match=re.escape(error)):
            await check


async def test_java_component_tests_run_in_process_without_a_network(tmp_path):
    repository, _ = java_contract(tmp_path, JAVA_STUB)
    suite = tmp_path / 'suite'
    (suite / 'src/test/java/com/example/flows').mkdir(parents=True)
    shutil.copy(JAVA.component_suite.template / 'pom.xml', suite / 'pom.xml')
    (suite / 'src/test/java/com/example/flows/FlowTest.java').write_text(JAVA_TEST)
    context = tmp_path / 'context'
    runner_context(JAVA.component_suite, repository, suite, context)
    image = await build_image(context, 'component test runner', tmp_path / 'build.log',
                              build_args={'PROJECT': 'service'})
    name = f'component-tests-{uuid.uuid4().hex}'
    try:
        await docker_agents.docker('create', '--name', name, '--network=none', image, timeout=30)
        await docker_agents.docker('start', name, timeout=30)
        await docker_agents.docker('wait', name, timeout=750)
        state = json.loads(await docker_agents.docker('inspect', '--format', '{{json .State}}',
                                                      name, timeout=30))
        await docker_agents.docker('cp', f'{name}:/tests/target/surefire-reports',
                                   str(tmp_path / 'reports'), timeout=30)
    finally:
        await docker_agents.docker('rm', '-f', name, timeout=30)
        await docker_agents.docker('image', 'rm', '-f', image, timeout=60)
    # The bug is reproduced: one failing test, reported as the verification reads it.
    assert state['ExitCode'] == 1
    report = (tmp_path / 'reports/TEST-junit-jupiter.xml').read_text()
    assert 'name="aClosedPullRequestCompletesTheFlow()"' in report and '<failure' in report


FRONTEND = Path(__file__).resolve().parents[2] / 'frontend'
TYPESCRIPT = get_part_language('typescript').component_contract


@pytest.mark.parametrize('stub, error', [
    (GITHUB_STUB, None),
    (GITHUB_STUB.replace('organization: string', 'organization: number'),
     "src/github: createGitHubApp: the stub's type is not assignable to the code's"),
    (GITHUB_STUB + 'export declare function reopen(): void;\n',
     'src/github: reopen is not exported by the code'),
])
async def test_typescript_stubs_conform_to_the_web_client_they_describe(tmp_path, stub, error):
    """This repository's web client, installed from its lock file and checked offline."""
    repository = tmp_path / 'repository'
    shutil.copytree(FRONTEND, repository / 'frontend',
                    ignore=shutil.ignore_patterns('node_modules', 'dist'))
    components = tmp_path / 'components'
    (components / 'src').mkdir(parents=True)
    (components / 'runtime.json').write_text(json.dumps({'project': 'frontend'}))
    (components / 'src/github.d.ts').write_text(stub)
    work = tmp_path / 'work'
    work.mkdir()
    check = check_conformance(TYPESCRIPT, repository, components, ['src/github'], work,
                              tmp_path / 'logs')
    if error is None:
        await check
    else:
        with pytest.raises(RuntimeError, match=re.escape(error)):
            await check


TYPESCRIPT_SUITE = get_part_language('typescript').component_suite


async def run_typescript_suite(tmp_path, repository: Path, suite: Path) -> tuple[dict, str]:
    context = tmp_path / f'context-{uuid.uuid4().hex}'
    runner_context(TYPESCRIPT_SUITE, repository, suite, context)
    image = await build_image(context, 'component test runner', tmp_path / 'build.log',
                              build_args=TYPESCRIPT.build_args({'project': 'frontend'}))
    name = f'component-tests-{uuid.uuid4().hex}'
    reports = tmp_path / f'reports-{uuid.uuid4().hex}'
    try:
        await docker_agents.docker('create', '--name', name, '--network=none', image, timeout=30)
        await docker_agents.docker('start', name, timeout=30)
        await docker_agents.docker('wait', name, timeout=750)
        state = json.loads(await docker_agents.docker('inspect', '--format', '{{json .State}}',
                                                      name, timeout=30))
        await docker_agents.docker('cp', f'{name}:/tests/reports', str(reports), timeout=30)
    finally:
        await docker_agents.docker('rm', '-f', name, timeout=30)
        await docker_agents.docker('image', 'rm', '-f', image, timeout=60)
    return state, (reports / 'junit.xml').read_text()


async def test_typescript_component_tests_prove_tr_6_red_then_green_without_a_network(tmp_path):
    """TR-6 of the flow-closing bugfix, against this repository's web client: failing as
    the code is, as the GitHub App manifest has no pull_request event, and passing once it
    has."""
    from tests.test_typescript_suite import MANIFEST_TEST
    repository = tmp_path / 'repository'
    shutil.copytree(FRONTEND, repository / 'frontend',
                    ignore=shutil.ignore_patterns('node_modules', 'dist'))
    suite = tmp_path / 'suite'
    copy_test_suite(TYPESCRIPT_SUITE, TYPESCRIPT_SUITE.template, suite)
    (suite / 'tests').mkdir()
    (suite / 'tests/manifest.test.ts').write_text(MANIFEST_TEST)
    name = 'name="the GitHub App manifest &gt; subscribes the app to pull_request events"'

    state, report = await run_typescript_suite(tmp_path, repository, suite)
    assert state['ExitCode'] == 1
    assert name in report and '<failure' in report

    github = repository / 'frontend/src/github.ts'
    events = '["issue_comment", "pull_request_review_comment"]'
    github.write_text(github.read_text().replace(
        events, '["issue_comment", "pull_request_review_comment", "pull_request"]'))
    state, report = await run_typescript_suite(tmp_path, repository, suite)
    assert state['ExitCode'] == 0
    assert name in report and '<failure' not in report


async def run_composite_suite(tmp_path, repository: Path, suite: Path) -> tuple[dict, str]:
    """The web client's tests in the runner of a suite of several parts, as a change whose
    criteria are all the web client's builds it: on the part's own runner image."""
    from app.languages.composite import composite_suite, SuiteMember
    from app.routing.parts import ComponentPart
    from app.test_generation.components import composite_files

    language = get_language('python')
    part = ComponentPart('frontend', 'frontend', get_part_language('typescript'))
    member, = composite_suite([SuiteMember('frontend', TYPESCRIPT_SUITE,
                                           label='TypeScript')]).members
    context = tmp_path / f'context-{uuid.uuid4().hex}'
    runner_context(TYPESCRIPT_SUITE, repository, suite, context)
    part_image = await build_image(context, 'component test runner', tmp_path / 'build.log',
                                   build_args=TYPESCRIPT.build_args({'project': 'frontend'}))
    tag = f'temporal-component-part:{uuid.uuid4().hex}-0'
    await docker_agents.docker('tag', part_image, tag, timeout=60)
    composite = tmp_path / f'composite-{uuid.uuid4().hex}' / 'context'
    composite.mkdir(parents=True)
    for name, text in composite_files(language, [(part, member)], {'frontend': tag}).items():
        (composite / name).write_text(text)
    name = f'component-tests-{uuid.uuid4().hex}'
    reports = tmp_path / f'reports-{uuid.uuid4().hex}'
    image = None
    try:
        image = await build_image(composite, 'component test runner',
                                  tmp_path / 'composite-build.log')
        await docker_agents.docker('create', '--name', name, '--network=none', image, timeout=30)
        await docker_agents.docker('start', name, timeout=30)
        await docker_agents.docker('wait', name, timeout=750)
        state = json.loads(await docker_agents.docker('inspect', '--format', '{{json .State}}',
                                                      name, timeout=30))
        await docker_agents.docker('cp', f'{name}:{language.test_reports.directory}',
                                   str(reports), timeout=30)
    finally:
        await docker_agents.docker('rm', '-f', name, timeout=30)
        for item in (image, tag, part_image):
            if item:
                await docker_agents.docker('image', 'rm', '-f', item, timeout=60)
    return state, (reports / 'junit-part-0-frontend.xml').read_text()


async def test_the_composite_runner_proves_tr_6_red_then_green_named_after_the_part(tmp_path):
    from tests.test_typescript_suite import MANIFEST_TEST
    repository = tmp_path / 'repository'
    shutil.copytree(FRONTEND, repository / 'frontend',
                    ignore=shutil.ignore_patterns('node_modules', 'dist'))
    suite = tmp_path / 'suite'
    copy_test_suite(TYPESCRIPT_SUITE, TYPESCRIPT_SUITE.template, suite)
    (suite / 'tests').mkdir()
    (suite / 'tests/manifest.test.ts').write_text(MANIFEST_TEST)
    classname = 'classname="frontend:tests/manifest.test.ts"'

    state, report = await run_composite_suite(tmp_path, repository, suite)
    assert state['ExitCode'] == 1
    assert classname in report and '<failure' in report

    github = repository / 'frontend/src/github.ts'
    events = '["issue_comment", "pull_request_review_comment"]'
    github.write_text(github.read_text().replace(
        events, '["issue_comment", "pull_request_review_comment", "pull_request"]'))
    state, report = await run_composite_suite(tmp_path, repository, suite)
    assert state['ExitCode'] == 0
    assert classname in report and '<failure' not in report
