"""Registry of the languages a service can be generated, tested and verified in, and of
the languages of the other parts of a repository that component contracts can describe.

To add one, create a package here that calls `register` with its `Language` (or
`register_part` with its `PartLanguage`) and import it at the bottom of this file.
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
    # What of a suite project `test_cases` cannot track, such as a test whose name is
    # computed, each said as the author is told it: a suite with any is rejected.
    untracked_tests: Callable[[Path], list[str]] = lambda project: []
    # Files of the template that are the trusted runner (its Dockerfile, .dockerignore and
    # scripts): the author can never replace them.
    runner: tuple[str, ...] = ('Dockerfile', '.dockerignore', 'run-tests.sh')

    def has_tests(self, project: Path) -> bool:
        """Whether the project holds a test file, as a complete suite does."""
        return any(project.glob(self.test_glob))


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
    # A part language's suite only: the shell command that runs it inside the runner of
    # a delivery's composite suite (app/languages/composite.py), where the part's runner
    # image's /repository, /tests and /runner are under `root`. It runs in the suite's
    # directory, tests the project at `project` within the repository, writes its JUnit
    # report to `report` and starts every class name of it with `prefix`. Every argument
    # comes from the host and is shell-quoted by it.
    part_command: Callable[[str, str, str, str], str] | None = None
    # A part language's suite only: files of its runner image that its command runs with,
    # such as its interpreter, which the composite runner copies to the same paths when it
    # is built on the service's runner. The first is run with `--version` when that image
    # is built, so a base without the libraries it needs fails the build, not the tests.
    part_runtime: tuple[str, ...] = ()


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
    # The inverse of `module`: the repository paths, by the normalized runtime file, that
    # may hold the source of a dotted module. None where the language cannot tell, so that
    # the host checks of a seam leave it to the review agent.
    source_paths: Callable[[str, dict], tuple[str, ...]] | None = None


@dataclass(frozen=True)
class StaticCheck:
    """A trusted tool run over the review workspace before the delivery review, whose
    results on the lines the delivery changed the reviewer judges."""
    # What the reviewer is told the result comes from.
    name: str
    # Shell command run in the language's agent image, without a network, in the
    # directory of the trees under review (`service/`, `tests/`, ...). It writes its
    # report to the file $REPORT and exits 0 whatever it found.
    command: str
    # The report as findings: dicts of `rule`, `path` (relative to that directory),
    # `line` and `message`, never the matched text itself.
    parse: Callable[[str], list[dict]]


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
    # Trusted tools whose results on the changed lines the delivery review is given.
    static_checks: tuple[StaticCheck, ...] = ()


@dataclass(frozen=True)
class PartLanguage:
    """The language of a part of a repository other than the service, such as a web
    client beside it, whose behaviour criteria may be observed at seams of its own code.
    A service language is not a part language: the service keeps its `Language`."""
    # The registry key, and what a part's routes record as its language.
    name: str
    # What the routing agent and the user are shown, such as "TypeScript".
    label: str
    # Whether the project in this directory of a checkout is one of the language's.
    detect: Callable[[Path], bool]
    # Directory names holding build output or installed dependencies of such a part.
    build_dirs: tuple[str, ...]
    component_contract: ComponentContract
    # Its runner is built with the part's sources under `repository/` and the contract's
    # build arguments, like a service language's.
    component_suite: ComponentSuite


# Dependencies and build output of the other projects a repository may hold beside the
# service, such as a web frontend: never sources, whatever the service's language.
OTHER_PARTS_BUILD_DIRS = ("node_modules", "dist", "__pycache__", ".venv", "venv")


class UnsupportedLanguage(ValueError):
    pass


_LANGUAGES: dict[str, Language] = {}
_PART_LANGUAGES: dict[str, PartLanguage] = {}


def register(language: Language) -> Language:
    if language.name in _LANGUAGES:
        raise ValueError(f"Language {language.name} is already registered.")
    _LANGUAGES[language.name] = language
    return language


def register_part(language: PartLanguage) -> PartLanguage:
    if language.name in _PART_LANGUAGES:
        raise ValueError(f"Part language {language.name} is already registered.")
    _PART_LANGUAGES[language.name] = language
    return language


def part_languages() -> list[PartLanguage]:
    return list(_PART_LANGUAGES.values())


def get_part_language(name: str) -> PartLanguage:
    language = _PART_LANGUAGES.get(name)
    if language is None:
        raise UnsupportedLanguage(f"Unsupported part language {name!r}.")
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


from app.languages import java, python, typescript  # noqa: E402,F401
