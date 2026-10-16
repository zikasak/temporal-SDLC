"""Python: FastAPI routers and Pydantic models, and pytest/httpx contract tests."""

import ast
import re
from pathlib import Path

from app.languages import Implementation, Language, TestReports, TestSuite, register

PACKAGE_NAME = "service"

IMPLEMENTATION_INSTRUCTIONS = """The service is a Python 3.12 FastAPI application.
Declare its dependencies in the project's requirements file and install them with
pip install --user to build it; check that every module compiles with python -m compileall
and that the application imports.
The Dockerfile installs the dependencies on python:3.12-slim, runs as a non-root user and
serves the application with uvicorn on 0.0.0.0:8080."""

TEST_SUITE_INSTRUCTIONS = """The suite is a pytest project using httpx for HTTP.
Write test modules named test_*.py under tests/, with test functions, or test methods of
Test* classes, named test_*. Read SERVICE_BASE_URL inside fixtures or tests, never at
import time, so that the suite can be collected without the service.
List any further dependency in requirements.txt and install it with pip install --user.
Check the suite with python -m pytest --collect-only -q and fix failures."""

TEST_GLOB = "tests/**/test_*.py"
TEST_FUNCTION = re.compile(r"^test")
TEST_CLASS = re.compile(r"^Test")


def _collect(body: list[ast.stmt], owner: str, context: str, cases: dict[str, str]) -> None:
    for node in body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and TEST_FUNCTION.match(node.name)):
            # With its decorators, which can change whether or how it runs; layout and
            # comments are not a change. A redefinition shares an identity.
            source = f"{context} {ast.unparse(node)}".strip()
            case = f"{owner}#{node.name}"
            cases[case] = f"{cases[case]} {source}" if case in cases else source
        elif isinstance(node, ast.ClassDef) and TEST_CLASS.match(node.name):
            # The class's decorators and bases apply to every test it holds.
            header = " ".join(ast.unparse(item)
                              for item in [*node.decorator_list, *node.bases, *node.keywords])
            _collect(node.body, f"{owner}.{node.name}",
                     f"{context} {node.name}({header})".strip(), cases)


def _test_cases(project: Path) -> dict[str, str]:
    """Every test of the project by `module.Class#test`, the identity pytest reports it
    under: the module is its path from the project, as pytest's rootdir is there."""
    cases: dict[str, str] = {}
    for file in sorted(project.glob(TEST_GLOB)):
        if file.is_symlink() or not file.is_file():
            continue
        text = file.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            # Collecting it fails the run; it holds no test until it parses.
            continue
        module = ".".join(file.relative_to(project).with_suffix("").parts)
        _collect(tree.body, module, "", cases)
    return cases


def _case_id(class_name: str, name: str) -> str:
    # pytest appends the parameter set to a parameterized test's name.
    return f"{class_name}#{name.partition('[')[0].strip()}"


def _generator_args() -> list[str]:
    return ["-g", "python-fastapi", "--additional-properties", f"packageName={PACKAGE_NAME}"]


PYTHON = register(Language(
    name="python", label="Python (FastAPI)",
    generator_args=_generator_args,
    build_dirs=("__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache",
                ".ruff_cache", "node_modules", "dist", "build"),
    implementation=Implementation(
        instructions=IMPLEMENTATION_INSTRUCTIONS,
        contract_paths=("src",), test_dirs=("test", "tests"),
    ),
    # pytest's JUnit XML report, as the test runner image writes it.
    test_reports=TestReports(directory="/tests/reports", pattern="junit*.xml"),
    test_suite=TestSuite(
        template=Path(__file__).parent / "test_suite",
        instructions=TEST_SUITE_INSTRUCTIONS,
        published=("tests", "pytest.ini", "requirements.txt", "README.md"),
        test_glob=TEST_GLOB, test_cases=_test_cases, case_id=_case_id,
        required=("pytest.ini", "requirements.txt"),
    ),
    # The shared agent image has Python 3.12 and pip.
    agent_dockerfile=Path(__file__).resolve().parents[2] / "agent_workflows" / "Dockerfile",
))
