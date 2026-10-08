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
        test_glob="src/test/java/**/*Test.java",
        required=("pom.xml",),
    ),
    agent_dockerfile=Path(__file__).resolve().parents[2] / "agent_workflows" / "Dockerfile",
))
