"""Registry of the languages a service can be generated, tested and verified in.

To add one, create a package here that calls `register` with its `Language` and
import it at the bottom of this file.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TestReports:
    """Where the language's test runner image leaves its JUnit XML reports."""
    # Absolute directory inside the runner container.
    directory: str
    # Glob for the report files directly inside it.
    pattern: str


@dataclass(frozen=True)
class Implementation:
    """How a service in the language is implemented from its generated code."""
    # What the implementation author is told about the stack, build and Dockerfile.
    instructions: str
    # Top-level paths of the generated code that are the API contract: in a repository
    # they replace the existing files, while other generated files only fill gaps.
    contract_paths: tuple[str, ...]
    # Directory names holding the service's own tests: never service sources.
    test_dirs: tuple[str, ...]


@dataclass(frozen=True)
class TestSuite:
    """The standalone HTTP contract test project written for a service in the language."""
    # Starting project. It also supplies the trusted runner files (Dockerfile,
    # .dockerignore and run-tests.sh), which the test author can never replace.
    template: Path
    # What the test author is told about the suite's stack, layout and build command.
    instructions: str
    # Files and directories of the project that are published; anything else is dropped.
    published: tuple[str, ...]
    # Glob, relative to the project, that at least one test file must match.
    test_glob: str
    # The test cases of a suite project: the identity of each, in the form `case_id`
    # gives, and its source, so that a changed test can be told from an untouched one.
    test_cases: Callable[[Path], dict[str, str]]
    # The identity of the test case a report entry (class name, test name) belongs to.
    case_id: Callable[[str, str], str]
    # Files a complete suite always has.
    required: tuple[str, ...] = ()
    # Files of the template that are the trusted runner (its Dockerfile, .dockerignore and
    # scripts): the author can never replace them.
    runner: tuple[str, ...] = ('Dockerfile', '.dockerignore', 'run-tests.sh')


@dataclass(frozen=True)
class ComponentSuite(TestSuite):
    """The standalone in-process test project of criteria routed to a seam: written from
    the component contract's stubs alone, with fakes of the collaborators they name, and
    run by the trusted runner, which builds it with the service's sources it never saw.

    The runner's build context holds the template's runner files, the service's sources
    under `repository/` and the suite's published files under `suite/`."""
    # Docker build arguments of the runner image, from the component contract's
    # normalized runtime file.
    build_args: Callable[[dict], dict[str, str]] = lambda runtime: {}
    # Trusted files the author checks the suite with, given to it in TOOLS_DIR.
    tools: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ComponentContract:
    """How a criterion the HTTP contract tests cannot observe is described at a seam of
    the service's code: stubs of existing symbols, with signatures, types and behaviour
    docstrings but no implementation, published under `contracts/components/`."""
    # What the routing agent is told about writing the stubs and checking them; formatted
    # with `runtime` (the runtime file's name).
    instructions: str
    # File name suffix of a stub; any other file of the contract but RUNTIME_FILE is
    # rejected.
    stub_suffix: str
    # The dotted module a stub at this path (relative to the contract) describes.
    module: Callable[[Path], str]
    # Checks the source of the stub of a module and returns the symbols it defines, by
    # dotted name within the module (`Class` and `Class.method`). Raises ValueError for a
    # body that is more than a signature, or anything else a stub may not hold.
    read_stub: Callable[[str, str], set[str]]
    # The top-level statements of a stub, comparable across versions of it, so a
    # published stub can be told to be kept or extended rather than changed.
    statements: Callable[[str], list[str]]
    # Checks the runtime file's object against the repository it describes and returns
    # it normalized. Raises ValueError for anything else.
    read_runtime: Callable[[object, Path], dict]
    # Trusted runner project (Dockerfile and script) of the conformance check: built with
    # the repository under `repository/` and the contract under `components/`, it exits
    # non-zero when a stub no longer matches the code.
    conformance: Path
    # Docker build arguments of the conformance image, from the normalized runtime file.
    build_args: Callable[[dict], dict[str, str]]
    # Trusted files the routing agent checks the contract with, given to it in TOOLS_DIR.
    tools: tuple[Path, ...] = ()


# The contract's file that says how to import the code the stubs describe.
RUNTIME_FILE = "runtime.json"
# Where an agent's workspace holds the trusted tools of its language's component contract.
TOOLS_DIR = "tools"


@dataclass(frozen=True)
class Language:
    # The registry key, and what a session records as its language.
    name: str
    # What the user is shown, such as "Java (Spring Boot)".
    label: str
    # openapi-generator arguments besides the input document and output directory.
    generator_args: Callable[[], list[str]]
    # Directory names holding build output or installed dependencies: never sources,
    # and never listed or searched by the coding agents.
    build_dirs: tuple[str, ...]
    # Read by verification once the language's test runner has finished.
    implementation: Implementation
    test_reports: TestReports
    test_suite: TestSuite
    # Image with the language's toolchain in which the coding agents write and build.
    agent_dockerfile: Path
    # Completes the generated project in place before it is published.
    post_process: Callable[[Path], None] = lambda project: None
    # None when criteria cannot be routed to a component seam in the language yet.
    component_contract: ComponentContract | None = None
    # The suite of the criteria routed to a seam; given with `component_contract`.
    component_suite: ComponentSuite | None = None


# Dependencies and build output of the other projects a repository may hold beside the
# service, such as a web frontend: never sources, whatever the service's language.
OTHER_PARTS_BUILD_DIRS = ("node_modules", "dist", "__pycache__", ".venv", "venv")


class UnsupportedLanguage(ValueError):
    pass


_LANGUAGES: dict[str, Language] = {}


def register(language: Language) -> Language:
    if language.name in _LANGUAGES:
        raise ValueError(f"Language {language.name} is already registered.")
    _LANGUAGES[language.name] = language
    return language


def supported_languages() -> list[Language]:
    return list(_LANGUAGES.values())


def describe_supported() -> str:
    """The supported languages as the model and the user are told them."""
    return ", ".join(f"{item.name} ({item.label})" for item in supported_languages())


def get_language(name: str | None) -> Language:
    language = _LANGUAGES.get((name or "").strip().lower())
    if language is None:
        raise UnsupportedLanguage(
            f"Unsupported language {name!r}. Supported: {describe_supported()}."
        )
    return language


from app.languages import java, python  # noqa: E402,F401
