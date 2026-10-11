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


from app.languages import java  # noqa: E402,F401
