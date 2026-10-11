"""Java: Spring API interfaces and models with a disabled JUnit stub per operation,
and Maven/JUnit 5 contract tests."""

import re
from pathlib import Path

from app.languages import Implementation, Language, TestReports, TestSuite, register
from app.config import CODEGEN_BASE_PACKAGE

SPRING_SERVER_PROPERTIES = {
    "interfaceOnly": "true",
    "useSpringBoot3": "true",
    "useTags": "true",
    "skipDefaultInterface": "true",
    "openApiNullable": "false",
    "documentationProvider": "none",
}


IMPLEMENTATION_INSTRUCTIONS = """The service is a Spring Boot application built with Maven.
Add the Spring Boot entry point and any needed dependencies/configuration.
Compile/package with Maven using -Dmaven.test.skip=true.
The Dockerfile uses a multi-stage Maven/Java build and starts the executable Spring Boot jar."""

TEST_SUITE_INSTRUCTIONS = """The suite is a Maven/JUnit 5 project.
Use Java 21 java.net.http.HttpClient, JUnit assertions and Jackson for JSON as needed.
Write test classes named *Test.java under src/test/java.
Compile with mvn -B -DskipTests -Dmaven.test.skip=false test-compile and fix failures."""

TEST_GLOB = "src/test/java/**/*Test.java"
PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
# An annotation or modifier that ends where a test annotation begins: part of the same
# method declaration, and able to change whether or how the test runs.
PRECEDING = re.compile(
    r"(?:@[\w.]+\s*(?:\((?:[^()]|\([^()]*\))*\))?"
    r"|\b(?:public|protected|private|static|final|abstract|synchronized))\s*$")
# How far back a declaration is searched for them.
DECLARATION_CHARS = 4000
TEST_ANNOTATION = re.compile(
    r"@(?:[\w.]+\.)?(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b")
INTERFACE = re.compile(r"^public interface (\w+)", re.MULTILINE)
OPERATION = re.compile(r"^    ResponseEntity<.*>\s+(\w+)\($", re.MULTILINE)
TEST_STUB = """package {package};

import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;

/**
 * Tests for {{@link {interface}}}. Enable each test once its operation is implemented.
 */
class {interface}Test {{
{methods}}}
"""
TEST_METHOD = """
    @Test
    @Disabled("Not implemented yet")
    void {operation}() {{
    }}
"""


def _test_method(text: str, start: int) -> tuple[str, int] | None:
    """The name of the method declared after the test annotation at `start`, and where
    its body ends. Annotation arguments, strings and comments are skipped over."""
    parentheses = braces = 0
    name = None
    index = start
    while index < len(text):
        if text.startswith("//", index):
            index = text.find("\n", index)
        elif text.startswith("/*", index):
            index = text.find("*/", index) + 1
        elif text.startswith('"""', index):
            index = text.find('"""', index + 3) + 2
        elif text[index] in "\"'":
            quote = text[index]
            index += 1
            while index < len(text) and text[index] != quote:
                index += 2 if text[index] == "\\" else 1
        elif text[index] == "(":
            if not parentheses and not braces:
                # The last one before the body is the parameter list.
                declared = re.search(r"(\w+)\s*$", text[start:index])
                name = declared.group(1) if declared else None
            parentheses += 1
        elif text[index] == ")":
            parentheses -= 1
        elif text[index] == ";" and not parentheses and not braces:
            return None
        elif text[index] == "{" and not parentheses:
            braces += 1
        elif text[index] == "}" and not parentheses:
            braces -= 1
            if not braces:
                return (name, index) if name else None
        if index < start:
            return None
        index += 1
    return None


def _declaration_start(text: str, start: int) -> int:
    """Where the method declaration holding the test annotation at `start` begins: before
    annotations such as @Disabled that precede it."""
    while found := PRECEDING.search(text, max(0, start - DECLARATION_CHARS), start):
        start = found.start()
    return start


def _test_cases(project: Path) -> dict[str, str]:
    """Every test method of the project by `Class#method`. Methods of nested classes
    count as their file's class, as `_case_id` reads reports."""
    cases: dict[str, str] = {}
    for file in sorted(project.glob(TEST_GLOB)):
        if file.is_symlink() or not file.is_file():
            continue
        text = file.read_text(encoding="utf-8", errors="replace")
        package = PACKAGE.search(text)
        owner = ".".join([*([package.group(1)] if package else []), file.stem])
        end = -1
        for annotation in TEST_ANNOTATION.finditer(text):
            method = _test_method(text, annotation.start()) if annotation.start() > end else None
            if not method:
                continue
            name, end = method
            # Overloads share an identity; layout alone is not a change.
            source = " ".join(text[_declaration_start(text, annotation.start()):end + 1].split())
            case = f"{owner}#{name}"
            cases[case] = f"{cases[case]} {source}" if case in cases else source
    return cases


def _case_id(class_name: str, name: str) -> str:
    # Surefire reports Outer$Inner for a nested class and appends the parameter types
    # and the invocation index to a parameterized test.
    return f"{class_name.partition('$')[0]}#{re.split(r'[(\[{]', name, maxsplit=1)[0].strip()}"


def _generator_args() -> list[str]:
    properties = ",".join(f"{key}={value}" for key, value in SPRING_SERVER_PROPERTIES.items())
    return [
        "-g", "spring",
        "--api-package", f"{CODEGEN_BASE_PACKAGE}.api",
        "--model-package", f"{CODEGEN_BASE_PACKAGE}.model",
        "--invoker-package", CODEGEN_BASE_PACKAGE,
        "--additional-properties", properties,
    ]


def _write_api_test_stubs(project: Path) -> None:
    """One JUnit class per generated API interface, one disabled test per operation."""
    package = f"{CODEGEN_BASE_PACKAGE}.api"
    relative = Path(*package.split("."))
    tests = project / "src" / "test" / "java" / relative
    tests.mkdir(parents=True, exist_ok=True)
    for source in sorted((project / "src" / "main" / "java" / relative).glob("*Api.java")):
        text = source.read_text(encoding="utf-8")
        interface = INTERFACE.search(text)
        if not interface:
            continue
        methods = "".join(TEST_METHOD.format(operation=name) for name in OPERATION.findall(text))
        (tests / f"{interface.group(1)}Test.java").write_text(
            TEST_STUB.format(package=package, interface=interface.group(1), methods=methods),
            encoding="utf-8",
        )


JAVA = register(Language(
    name="java", label="Java (Spring Boot)",
    generator_args=_generator_args, post_process=_write_api_test_stubs,
    build_dirs=("target",),
    implementation=Implementation(
        instructions=IMPLEMENTATION_INSTRUCTIONS,
        contract_paths=("src",), test_dirs=("test", "tests"),
    ),
    # Maven Surefire, as configured by the test runner image.
    test_reports=TestReports(directory="/tests/target/surefire-reports", pattern="TEST-*.xml"),
    test_suite=TestSuite(
        template=Path(__file__).parent / "test_suite",
        instructions=TEST_SUITE_INSTRUCTIONS,
        published=("src/test", "pom.xml", "README.md"),
        test_glob=TEST_GLOB, test_cases=_test_cases, case_id=_case_id,
        required=("pom.xml",),
    ),
    agent_dockerfile=Path(__file__).resolve().parents[2] / "agent_workflows" / "Dockerfile",
))
