"""Component contracts and component tests of Java services: stubs read on the host, the
trusted Stubs.java that compares them with the compiled code, and the author of the
component tests, which sees the stubs and the tool, never the code."""
# Assertions read optional fields directly: a None there fails the test anyway.
# pyright: reportOptionalMemberAccess=false
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.api_design.models import empty_api
from app.languages import get_language
from app.languages.java import components as java_components
from app.test_generation import activities as contract_tests
from app.test_generation import components
from app.verification import activities as verification

JAVA = get_language('java')
assert JAVA.component_contract is not None and JAVA.component_suite is not None
CONTRACT = JAVA.component_contract
SUITE = JAVA.component_suite
STUBS = java_components.STUBS_TOOL

WORKFLOW = '''package com.example.flows;

import java.util.List;

/** The delivery flow of one session. */
public interface FlowWorkflow {
    /** The flow's pull request was closed, merged or not: the flow completes. */
    void pullRequestClosed(boolean merged);

    /** The flow's current state. */
    FlowState getState();

    int MAX_SESSIONS = 3;

    /** How a flow ended. */
    enum Outcome { MERGED, CLOSED }

    /** What a flow's query returns. */
    record FlowState(String status, List<String> sessions) {
        /** Whether a session of the flow can still be forked. */
        public boolean forkable();
    }
}
'''
IMPLEMENTATION = '''package com.example.flows;

import java.util.List;
import java.util.Map;

/** The workflow's implementation, which tests register with a worker. */
public class FlowWorkflowImpl implements FlowWorkflow {
    protected static final String NAME;
    private final List<String> sessions;

    public FlowWorkflowImpl(String sessionId);

    @Override
    public void pullRequestClosed(boolean merged);

    @Override
    public FlowState getState();

    public <T extends Comparable<? super T>> Map<String, List<? extends T>> group(
            List<T> items, String... keys) throws java.io.IOException;
}
'''


def symbols(source: str, module: str = 'com.example.flows.FlowWorkflow') -> list[str]:
    return sorted(CONTRACT.read_stub(module, source))


def test_a_stub_declares_its_types_members_by_their_names_within_its_type():
    assert CONTRACT.module(Path('com/example/flows/FlowWorkflow.java')) == \
        'com.example.flows.FlowWorkflow'
    assert symbols(WORKFLOW) == [
        'FlowWorkflow', 'FlowWorkflow.FlowState', 'FlowWorkflow.FlowState.forkable',
        'FlowWorkflow.FlowState.sessions', 'FlowWorkflow.FlowState.status',
        'FlowWorkflow.MAX_SESSIONS', 'FlowWorkflow.Outcome', 'FlowWorkflow.Outcome.CLOSED',
        'FlowWorkflow.Outcome.MERGED', 'FlowWorkflow.getState',
        'FlowWorkflow.pullRequestClosed']
    # A constructor is named like its type; overloads share their method's name.
    assert symbols(IMPLEMENTATION, 'com.example.flows.FlowWorkflowImpl') == [
        'FlowWorkflowImpl', 'FlowWorkflowImpl.FlowWorkflowImpl', 'FlowWorkflowImpl.NAME',
        'FlowWorkflowImpl.getState', 'FlowWorkflowImpl.group',
        'FlowWorkflowImpl.pullRequestClosed', 'FlowWorkflowImpl.sessions']


@pytest.mark.parametrize('source', [
    'package p;\n@io.x.Ann(name = "a", values = {1, 2}) public @interface A {\n'
    '    String value() default "";\n    Class<?> type() default Object.class;\n}\n',
    'package p;\npublic enum A { /** One. */ X(1), Y(2); A(int value); public int value(); }\n',
    'package p;\nclass A { static final int X = 1 << 3, Y = (int) 2L; '
    'static final String T = """\n    text "quoted"\n    """; }\n',
    'package p;\npublic sealed interface A permits A.B { non-sealed class B implements A { } }\n',
    'package p;\nclass A { void f(@Ann(x = 1) java.util.List<? super String> items); }\n',
])
def test_any_declaration_java_has_may_be_stubbed(source):
    assert 'A' in CONTRACT.read_stub('p.A', source)


@pytest.mark.parametrize('source, error', [
    ('package p; class A { void f() { } }', 'the body of f is more than `;`'),
    ('package p; class A { A() { super(); } }', 'the body of A is more than `;`'),
    ('package p; class A { static { } }', 'no initializer block'),
    ('package p; record A(int a) { A { } }', 'holds no body'),
    ('package p; class A { Runnable r = () -> null; }', 'holds no code (->)'),
    ('package p; class A { Object o = new Object(); }', 'holds no code (new)'),
    ('package p; class A { int x = compute(); }', 'a call of compute'),
    ('package p; class A { @Ann(value = compute()) void f(); }', 'a call of compute'),
    ('package p; enum A { X { }; }', 'no enum constant body'),
    ('package p; class A { } class B { }', 'declares one top-level type'),
    ('package q; class A { }', 'its package is q, but its path is that of p'),
    ('package p; class B { }', 'does not declare A'),
    ('package p; class A { String s = "open; }', 'unterminated literal'),
    ('package p; class A { /* open', 'unterminated comment'),
    ('package p; import java.util.*;', 'declares no type'),
])
def test_a_stub_holds_declarations_only(source, error):
    with pytest.raises(ValueError, match=re.escape(error)):
        CONTRACT.read_stub('p.A', source)


def test_a_published_stub_may_gain_declarations_but_keeps_every_one_it_has():
    published = CONTRACT.statements(WORKFLOW)
    extended = WORKFLOW.replace('    int MAX_SESSIONS = 3;\n', '''    int MAX_SESSIONS = 3;

    /** Reopens the flow. */
    void reopen();
''').replace('import java.util.List;\n', 'import java.util.List;\nimport java.util.Map;\n')
    assert all(statement in CONTRACT.statements(extended) for statement in published)
    # A changed signature, enum constant or docstring is a changed declaration.
    for changed in (WORKFLOW.replace('boolean merged', 'boolean merged, String why'),
                    WORKFLOW.replace('MERGED, CLOSED', 'MERGED'),
                    WORKFLOW.replace('merged or not: the flow completes', 'merged')):
        assert any(statement not in CONTRACT.statements(changed) for statement in published)
    # Layout and comments are not a change.
    relaid = WORKFLOW.replace('void pullRequestClosed(boolean merged);',
                              'void pullRequestClosed(  // the signal\n        boolean merged);')
    assert CONTRACT.statements(relaid) == published


def test_the_runtime_file_names_the_maven_project_that_compiles_the_stubbed_code(tmp_path):
    (tmp_path / 'service').mkdir()
    (tmp_path / 'service/pom.xml').write_text('<project/>')
    assert CONTRACT.read_runtime({'project': 'service'}, tmp_path) == {'project': 'service'}
    assert CONTRACT.build_args({'project': 'service'}) == {'PROJECT': 'service'}
    (tmp_path / 'linked').symlink_to(tmp_path / 'service')
    for runtime, error in [({'project': '.'}, 'with a pom.xml'),
                           ({'project': '../service'}, 'inside the repository'),
                           ({'project': 'linked'}, 'with a pom.xml'),
                           ({'project': 'service', 'source_root': '.'}, 'exactly project')]:
        with pytest.raises(ValueError, match=error):
            CONTRACT.read_runtime(runtime, tmp_path)


def test_both_java_runners_build_the_code_alike():
    assert ((SUITE.template / 'build.sh').read_text()
            == (CONTRACT.conformance / 'build.sh').read_text())
    assert all((SUITE.template / name).is_file() for name in SUITE.runner)
    assert {'Dockerfile', 'check.sh', 'build.sh', 'Stubs.java'} <= {
        item.name for item in CONTRACT.conformance.iterdir()}


def test_the_java_component_suite_template_publishes_no_runner(tmp_path):
    contract_tests.copy_test_suite(SUITE, SUITE.template, tmp_path / 'suite')
    assert sorted(item.name for item in (tmp_path / 'suite').iterdir()) == ['README.md',
                                                                         'pom.xml']


def test_the_console_launchers_report_names_the_tests_the_suite_holds(tmp_path):
    source = tmp_path / 'src/test/java/com/example/flows/FlowWorkflowTest.java'
    source.parent.mkdir(parents=True)
    source.write_text('''package com.example.flows;

class FlowWorkflowTest {
    @ParameterizedTest
    @ValueSource(booleans = {true, false})
    void aClosedPullRequestCompletesTheFlow(boolean merged) { }

    @Nested
    class Forking {
        @Test
        void noSessionOfACompletedFlowCanBeForked() { }
    }
}
''')
    # As JUnit's legacy XML report names them.
    reported = {SUITE.case_id('com.example.flows.FlowWorkflowTest',
                              'aClosedPullRequestCompletesTheFlow(boolean)[2]'),
                SUITE.case_id('com.example.flows.FlowWorkflowTest$Forking',
                              'noSessionOfACompletedFlowCanBeForked()')}
    assert reported == set(SUITE.test_cases(tmp_path))


# ---- the author of Java component tests ----

REQUIREMENT = {'category': 'functional', 'title': 'T', 'description': 'D', 'priority': 'must'}
BRIEF = {'requirements': [{**REQUIREMENT, 'id': 'FR-3'}], 'requirements_document': '# Brief',
         'retired': []}
ROUTES = {'FR-3': {'route': 'component', 'reason': 'Signal, then query.', 'seam': {
    'module': 'com.example.flows.FlowWorkflow',
    'symbols': ['FlowWorkflow.pullRequestClosed', 'FlowWorkflow.getState']}}}
CONTEXT = {'requirements': [], 'requirements_document': '# Contract', 'language': 'java',
           'api_design': empty_api(), 'brief': BRIEF, 'routes': ROUTES,
           'repository': {'full_name': 'octo/flows'}}
TEST = '''package com.example.flows;

import org.junit.jupiter.api.Test;

class FlowWorkflowTest {
    @Test
    void aMergedPullRequestCompletesTheFlow() { }
}
'''


@pytest.fixture
async def session(tmp_path, monkeypatch):
    for module in (components, verification, contract_tests):
        monkeypatch.setattr(module, 'GENERATED_CODE_DIR', tmp_path)
    session_dir = tmp_path / 'session'
    for path, text in {
        'repository/Dockerfile': 'FROM eclipse-temurin',
        'repository/service/pom.xml': '<project/>',
        'repository/service/src/main/java/com/example/flows/FlowWorkflowImpl.java':
            'SECRET-SERVICE-SOURCE',
        'routing/components/runtime.json': json.dumps({'project': 'service'}),
        'routing/components/com/example/flows/FlowWorkflow.java': WORKFLOW,
    }.items():
        (session_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (session_dir / path).write_text(text)
    assert await ActivityEnvironment().run(verification.snapshot_repository_tests, 'session',
                                           'java') == {'tests': 0, 'component_tests': 0}
    return session_dir


async def test_the_java_author_gets_the_stubs_and_the_tool_and_the_runner_builds_the_project(
        session, monkeypatch):
    sent: dict = {}
    builds: list = []

    async def docker(*args, **kwargs):
        if args[0] == 'inspect':
            return json.dumps({'ExitCode': 0})
        if args[0] == 'cp' and args[1] == '-a':
            with tarfile.open(fileobj=kwargs['stdin']) as archive:
                sent.update({item.name: archive.extractfile(item).read().decode()
                             for item in archive.getmembers() if item.isfile()})
        if args[0] == 'cp' and args[1].endswith(':/workspace/tests/.'):
            output = Path(args[2])
            for path, text in {'src/test/java/com/example/flows/FlowWorkflowTest.java': TEST,
                               'pom.xml': '<project/>', 'build.sh': 'untrusted'}.items():
                (output / path).parent.mkdir(parents=True, exist_ok=True)
                (output / path).write_text(text)
        if args[0] == 'build' and '--iidfile' in args:
            context = Path(args[-1])
            builds.append({'args': args, 'files': {
                item.relative_to(context).as_posix(): item.read_text()
                for item in context.rglob('*') if item.is_file()}})
            Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'e' * 64)
        return ''

    monkeypatch.setattr(docker_agents, 'docker', docker)
    result = await ActivityEnvironment().run(components.generate_component_tests, 'session',
                                             CONTEXT)
    assert sent['contracts/components/com/example/flows/FlowWorkflow.java'] == WORKFLOW
    assert sent['tools/Stubs.java'] == STUBS.read_text()
    assert sent['tests/pom.xml'] == (SUITE.template / 'pom.xml').read_text()
    assert not any('SECRET-SERVICE-SOURCE' in text for text in sent.values())
    assert json.loads(sent['language.json'])['instructions'] == SUITE.instructions
    build, = builds
    for name in SUITE.runner:
        assert build['files'][name] == (SUITE.template / name).read_text()
    assert 'suite/build.sh' not in build['files']
    assert build['files']['repository/service/src/main/java/com/example/flows/'
                          'FlowWorkflowImpl.java'] == 'SECRET-SERVICE-SOURCE'
    assert 'PROJECT=service' in build['args']
    assert result['added_tests'] == ['com.example.flows.FlowWorkflowTest#'
                                     'aMergedPullRequestCompletesTheFlow']


# ---- Stubs.java, run with the JDK when one is installed ----

def jdk() -> bool:
    if not shutil.which('javac') or not shutil.which('java'):
        return False
    version = subprocess.run(['javac', '-version'], capture_output=True, text=True).stdout
    found = re.search(r'(\d+)', version)
    return bool(found) and int(found.group(1)) >= 21


needs_jdk = pytest.mark.skipif(not jdk(), reason='needs a JDK 21 or newer')

CODE = {
    'com/example/flows/FlowWorkflow.java': '''package com.example.flows;

import java.util.List;

public interface FlowWorkflow {
    void pullRequestClosed(boolean merged);

    FlowState getState();

    int MAX_SESSIONS = 3;

    enum Outcome { MERGED, CLOSED }

    record FlowState(String status, List<String> sessions) {
        public boolean forkable() { return status.equals("open"); }
    }
}
''',
    'com/example/flows/FlowWorkflowImpl.java': '''package com.example.flows;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

public class FlowWorkflowImpl implements FlowWorkflow {
    protected static final String NAME = "flow";
    private final List<String> sessions = new ArrayList<>();
    private String status = "open";

    public FlowWorkflowImpl(String sessionId) { sessions.add(sessionId); }

    @Override
    public void pullRequestClosed(boolean merged) { }

    @Override
    public FlowState getState() { return new FlowState(status, List.copyOf(sessions)); }

    public <T extends Comparable<? super T>> Map<String, List<? extends T>> group(
            List<T> items, String... keys) throws java.io.IOException { return Map.of(); }

    void unstubbed() { }
}
''',
}
MODULES = ['com.example.flows.FlowWorkflow', 'com.example.flows.FlowWorkflowImpl']


def compiled_code(tmp_path) -> Path:
    sources = tmp_path / 'code'
    for path, text in CODE.items():
        (sources / path).parent.mkdir(parents=True, exist_ok=True)
        (sources / path).write_text(text)
    subprocess.run(['javac', '-d', str(tmp_path / 'classes'),
                    *map(str, sources.rglob('*.java'))], check=True)
    (tmp_path / 'dependencies.txt').write_text('')
    return tmp_path / 'classes'


def check(tmp_path, stubs: dict[str, str]) -> subprocess.CompletedProcess:
    contract = tmp_path / 'components'
    shutil.rmtree(contract, ignore_errors=True)
    for path, text in stubs.items():
        (contract / path).parent.mkdir(parents=True, exist_ok=True)
        (contract / path).write_text(text)
    return subprocess.run(['java', str(STUBS), 'check', str(contract), str(tmp_path / 'classes'),
                           str(tmp_path / 'dependencies.txt'), *MODULES],
                          capture_output=True, text=True, timeout=120)


STUBBED = {'com/example/flows/FlowWorkflow.java': WORKFLOW,
           'com/example/flows/FlowWorkflowImpl.java': IMPLEMENTATION}


@needs_jdk
def test_stubs_that_describe_the_compiled_code_conform(tmp_path):
    compiled_code(tmp_path)
    result = check(tmp_path, STUBBED)
    assert (result.returncode, result.stdout, result.stderr) == (0, '', '')


@needs_jdk
@pytest.mark.parametrize('stub, old, new, error', [
    # A seam the code does not have yet, as routing reports a symbol a change adds.
    ('FlowWorkflowImpl', '    @Override\n    public FlowState getState();',
     '    @Override\n    public FlowState getState();\n\n    public void reopen();',
     'error: com.example.flows.FlowWorkflowImpl.reopen() is not present at runtime'),
    ('FlowWorkflowImpl', 'public FlowWorkflowImpl(String sessionId);',
     'public FlowWorkflowImpl(int sessionId);',
     'FlowWorkflowImpl.FlowWorkflowImpl(int) is not present at runtime'),
    ('FlowWorkflowImpl', 'Map<String, List<? extends T>>', 'Map<String, List<T>>',
     'return type differs'),
    ('FlowWorkflowImpl', ' throws java.io.IOException;', ';', 'thrown types differ'),
    ('FlowWorkflowImpl', 'protected static final String NAME;',
     'public static final String NAME;', 'NAME is inconsistent: modifiers differ'),
    ('FlowWorkflowImpl', 'public class FlowWorkflowImpl',
     'public class FlowWorkflowImpl extends Exception', 'superclass differs'),
    ('FlowWorkflow', 'MERGED, CLOSED', 'MERGED, CLOSED, ABANDONED',
     'FlowWorkflow.Outcome.ABANDONED is not present at runtime'),
    ('FlowWorkflow', 'List<String> sessions)', 'List<Object> sessions)',
     'record components differ'),
    ('FlowWorkflow', 'public interface FlowWorkflow', '@Deprecated public interface FlowWorkflow',
     'is not annotated @java.lang.Deprecated'),
])
def test_a_stub_that_differs_from_the_code_is_named_with_the_difference(
        tmp_path, stub, old, new, error):
    compiled_code(tmp_path)
    path = f'com/example/flows/{stub}.java'
    assert old in STUBBED[path]
    result = check(tmp_path, {**STUBBED, path: STUBBED[path].replace(old, new)})
    assert result.returncode == 1 and error in result.stdout, result.stdout + result.stderr


@needs_jdk
def test_stubs_name_no_type_of_the_code_they_do_not_stub(tmp_path):
    compiled_code(tmp_path)
    result = check(tmp_path, {'com/example/flows/FlowWorkflowImpl.java': IMPLEMENTATION})
    assert result.returncode == 1
    assert 'stub every type of the code they name' in result.stdout
    assert 'FlowWorkflowImpl.java:7: cannot find symbol' in result.stdout


@needs_jdk
def test_skeletons_of_the_stubs_compile_and_run_nothing_of_the_code(tmp_path):
    contract = tmp_path / 'components'
    for path, text in STUBBED.items():
        (contract / path).parent.mkdir(parents=True, exist_ok=True)
        (contract / path).write_text(text)
    subprocess.run(['java', str(STUBS), 'skeletons', str(contract), str(tmp_path / 'skeletons')],
                   check=True, timeout=120)
    skeleton = (tmp_path / 'skeletons/com/example/flows/FlowWorkflowImpl.java').read_text()
    assert 'protected static final String NAME = null;' in skeleton
    assert ('public FlowWorkflowImpl(String sessionId) '
            '{ throw new UnsupportedOperationException(); }') in skeleton
    subprocess.run(['javac', '-d', str(tmp_path / 'out'),
                    *map(str, (tmp_path / 'skeletons').rglob('*.java'))], check=True)
