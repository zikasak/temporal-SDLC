"""Python: FastAPI routers and Pydantic models, and pytest/httpx contract tests."""

import ast
import re
from pathlib import Path

from app.languages import (
    ComponentSuite, Implementation, Language, TestReports, TestSuite, register,
)
from app.languages.python.components import COMPONENT_CONTRACT, MYPY, runner_build_args
from app.languages.static_checks import GITLEAKS, RUFF

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

COMPONENT_SUITE_INSTRUCTIONS = """The suite is a pytest project with pytest-asyncio
(asyncio_mode = auto, so a test may be an async def). Write test modules named test_*.py
under tests/, with test functions, or test methods of Test* classes, named test_*. Each
test imports the stubbed modules by their dotted names, as the code under test is
importable when the suite runs, and calls only the symbols the stubs declare, with the
signatures they declare. Write each fake of a collaborator in the suite itself, with the
signature its stub gives it. When a seam is a Temporal workflow, start an in-process
`temporalio.testing.WorkflowEnvironment` (`start_time_skipping()`, or `start_local()` when
the test sends an update), run a `temporalio.worker.Worker` on it with the stubbed workflow
and your fakes of its activities, registered under the activity names the stubs give, and
drive the workflow through its stubbed signals, updates and queries. Both environments'
servers are fetched when the suite is built and work without a network.
Each test is stopped after 30 seconds and the whole suite after ten minutes, and every test
you add first runs against the code without the change, where it waits out each of its
bounds: keep a wait for something that may never happen to a few seconds, and prefer the
time-skipping environment, whose timers do not take real time. A test that outlasts the
limit proves nothing and is sent back to you. So wrap every await on the workflow that the
code without the change may never answer, such as `handle.result()`, a loop polling a
query or `handle.describe()` until its status changes, or an `asyncio.Event` or queue a
fake fills, in `asyncio.wait_for(..., timeout=5)` or a loop with a deadline, and turn the
timeout into an assertion failure that says what did not happen. A fake of an activity
answers at once and never waits for something a test may not provide.
List any further dependency of the tests in requirements.txt; never list the service or
its own dependencies, which the runner installs from the service's requirements.
The service's code is not available here, so the suite cannot be collected or run. Check
it from /workspace/tests instead: python -m compileall -q tests, then
pip install --user {mypy} and the packages the stubs import, and
MYPYPATH=/workspace/contracts/components python -m mypy --ignore-missing-imports
--follow-imports=silent tests, and fix every error about a stubbed symbol."""

TEST_GLOB = "tests/**/test_*.py"
TEST_FUNCTION = re.compile(r"^test")
TEST_CLASS = re.compile(r"^Test")


# How many imports and bases deep a base class is looked for.
MAX_DEPTH = 20
TestMethod = ast.FunctionDef | ast.AsyncFunctionDef


class _Modules:
    """The suite's own modules by dotted path from the project, parsed when first needed,
    and the classes their names refer to. Anything from outside the suite is unknown."""

    def __init__(self, project: Path) -> None:
        self.project = project
        self.trees: dict[str, ast.Module | None] = {}

    def tree(self, module: str) -> ast.Module | None:
        if module not in self.trees:
            file = self.project.joinpath(*module.split(".")).with_suffix(".py")
            tree = None
            if module and file.is_file() and not file.is_symlink():
                try:
                    tree = ast.parse(file.read_text(encoding="utf-8", errors="replace"))
                except (SyntaxError, ValueError):
                    # Collecting it fails the run; it holds no test until it parses.
                    pass
            self.trees[module] = tree
        return self.trees[module]

    @staticmethod
    def _imported(module: str, statement: ast.ImportFrom) -> list[str]:
        """Where `from … import` in `module` may import from: relative to its package,
        or, as pytest puts the test file's directory on the path, beside it or from the
        project's root."""
        package = module.split(".")[:-1]
        target = statement.module.split(".") if statement.module else []
        if statement.level:
            return [".".join([*package[:len(package) - statement.level + 1], *target])]
        return [".".join([*package, *target]), ".".join(target)]

    def resolve(self, module: str, name: ast.expr,
                depth: int = 0) -> tuple[str, ast.ClassDef] | None:
        """The class `name`, a base in `module`, refers to, and the module it is in."""
        tree = self.tree(module)
        if tree is None or depth > MAX_DEPTH:
            return None
        if isinstance(name, ast.Attribute) and isinstance(name.value, ast.Name):
            # A class of an imported module: `import base` and `base.Shared`.
            for statement in tree.body:
                if isinstance(statement, ast.Import):
                    for alias in statement.names:
                        if (alias.asname or alias.name) == name.value.id:
                            return self.resolve(alias.name, ast.Name(name.attr), depth + 1)
            return None
        if not isinstance(name, ast.Name):
            return None
        found = None
        for statement in tree.body:
            if isinstance(statement, ast.ClassDef) and statement.name == name.id:
                found = module, statement
            elif isinstance(statement, ast.ImportFrom):
                for alias in statement.names:
                    if (alias.asname or alias.name) == name.id:
                        found = next(filter(None, (
                            self.resolve(source, ast.Name(alias.name), depth + 1)
                            for source in self._imported(module, statement))), None)
        return found

    def lineage(self, module: str, node: ast.ClassDef, depth: int = 0) -> str:
        """The decorators and bases of a class and of every class of the suite it inherits
        from: pytest applies a base's marks to its subclasses too."""
        parents = [self.lineage(*parent, depth + 1) for base in node.bases
                   if depth < MAX_DEPTH and (parent := self.resolve(module, base))]
        header = " ".join(ast.unparse(item)
                          for item in [*node.decorator_list, *node.bases, *node.keywords])
        return " ".join([f"{node.name}({header})", *parents])

    def members(self, module: str, node: ast.ClassDef,
                depth: int = 0) -> tuple[dict[str, str], dict[str, tuple[str, ast.ClassDef]]]:
        """What pytest collects of a class, its own before what it inherits from classes of
        the suite: its test methods, each with its decorators (a redefinition shares an
        identity), and its nested test classes with the module each is in."""
        methods: dict[str, str] = {}
        classes: dict[str, tuple[str, ast.ClassDef]] = {}
        for item in node.body:
            if isinstance(item, TestMethod) and TEST_FUNCTION.match(item.name):
                source = ast.unparse(item)
                methods[item.name] = (f"{methods[item.name]} {source}" if item.name in methods
                                      else source)
            elif isinstance(item, ast.ClassDef) and TEST_CLASS.match(item.name):
                classes[item.name] = module, item
        for base in node.bases if depth < MAX_DEPTH else []:
            if parent := self.resolve(module, base):
                inherited_methods, inherited_classes = self.members(*parent, depth + 1)
                methods = {**inherited_methods, **methods}
                classes = {**inherited_classes, **classes}
        return methods, classes


def _collect_class(modules: _Modules, module: str, node: ast.ClassDef, owner: str,
                   context: str, cases: dict[str, str], depth: int = 0) -> None:
    owner = f"{owner}.{node.name}"
    # A nested class runs under its outer classes' decorators and bases too.
    context = f"{context} {modules.lineage(module, node)}".strip()
    methods, classes = modules.members(module, node)
    for name, source in methods.items():
        cases[f"{owner}#{name}"] = f"{context} {source}"
    for inner in classes.values() if depth < MAX_DEPTH else []:
        _collect_class(modules, *inner, owner, context, cases, depth + 1)


def _collect(modules: _Modules, module: str, tree: ast.Module, cases: dict[str, str]) -> None:
    """The tests of a module with the source each is compared by: the test with its
    decorators, which can change whether or how it runs, after the decorators and bases of
    the classes it runs in. Layout and comments are not a change."""
    for node in tree.body:
        if isinstance(node, TestMethod) and TEST_FUNCTION.match(node.name):
            case = f"{module}#{node.name}"
            source = ast.unparse(node)
            cases[case] = f"{cases[case]} {source}" if case in cases else source
        elif isinstance(node, ast.ClassDef) and TEST_CLASS.match(node.name):
            _collect_class(modules, module, node, module, "", cases)


def _test_cases(project: Path) -> dict[str, str]:
    """Every test of the project by `module.Class#test`, the identity pytest reports it
    under: the module is its path from the project, as pytest's rootdir is there. A class
    holds the test methods it inherits from classes of the suite, as pytest runs them."""
    cases: dict[str, str] = {}
    modules = _Modules(project)
    for file in sorted(project.glob(TEST_GLOB)):
        if file.is_symlink() or not file.is_file():
            continue
        module = ".".join(file.relative_to(project).with_suffix("").parts)
        tree = modules.tree(module)
        if tree is not None:
            _collect(modules, module, tree, cases)
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
    component_contract=COMPONENT_CONTRACT,
    static_checks=(GITLEAKS, RUFF),
    component_suite=ComponentSuite(
        template=Path(__file__).parent / "component_suite",
        instructions=COMPONENT_SUITE_INSTRUCTIONS.replace("{mypy}", MYPY),
        published=("tests", "pytest.ini", "requirements.txt", "README.md"),
        test_glob=TEST_GLOB, test_cases=_test_cases, case_id=_case_id,
        required=("pytest.ini", "requirements.txt"),
        runner=("Dockerfile", ".dockerignore", "run-tests.sh", "prepare.py"),
        build_args=runner_build_args,
    ),
))
