"""Opt-in check of the component contracts' conformance runners and the Java component
test runner with real Docker.

Run: RUN_DOCKER_VERIFICATION_TESTS=1 python -m pytest tests/test_component_contract_docker.py -q
Requires Docker and outbound access to fetch the base images, mypy and Maven artifacts.
"""
import json
import os
import re
import shutil
import uuid

import pytest

from app import docker_agents
from app.agent_workflows.host import build_image
from app.languages import get_language
from app.routing.components import check_conformance
from app.test_generation.components import runner_context

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
