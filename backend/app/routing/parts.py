"""The parts of a repository whose criteria a component contract can describe: the service,
in the session's language, and the projects beside it, such as a web client, in a
registered part language (plans/part-component-contracts.md)."""
import json
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any

from app.agent_workflows.parts import repository_parts
from app.github.checkout import (
    BROWSER_DIR, BROWSER_TESTS_DIR, COMPONENT_TESTS_DIR, COMPONENTS_DIR, CONTRACTS_DIR,
)
from app.languages import (
    BrowserContract, ComponentContract, ComponentSuite, Language, PartLanguage,
    UnsupportedLanguage, get_part_language, part_languages,
)
from app.asyncio_utils import run_sync
from app.languages.composite import (
    BROWSER_MEMBER, BROWSER_SUFFIX, PARTS_DIR as SUITE_PARTS_DIR, SuiteMember, composite_suite,
)
from app.routing.components import check_conformance, stub_modules

# The part a seam is in when it names none.
SERVICE_PART = 'service'
# Where the contracts of the other parts live: contracts/parts/P/components/ in the
# repository, parts/P/components/ beside the routing and in the routing agent's workspace,
# and their page contracts beside them, in parts/P/browser/.
PARTS_DIR = 'parts'
# The kind of a part's record in the routes when it is the browser tests of a part.
BROWSER_KIND = 'browser'


@dataclass(frozen=True)
class ComponentPart:
    # SERVICE_PART, or the part's directory in the repository, which is its name.
    name: str
    # The part's directory in the repository (`.` for the service).
    root: str
    # The service's Language, or the part's PartLanguage.
    language: Language | PartLanguage
    # The browser tests of a part, at pages of its web client, rather than its component
    # tests at seams of its code: one part may have both, as two ComponentParts.
    browser: bool = False

    @property
    def is_service(self) -> bool:
        return self.name == SERVICE_PART and not self.browser

    @property
    def key(self) -> str:
        """The part's name among the members of a delivery's suite: its name, and for its
        browser tests its name with BROWSER_SUFFIX (`frontend~browser`)."""
        return f'{self.name}{BROWSER_SUFFIX}' if self.browser else self.name

    @property
    def label(self) -> str:
        """How the user is told of the part, such as `frontend (browser)`."""
        return f'{self.name} (browser)' if self.browser else self.name

    @property
    def contract(self) -> ComponentContract | None:
        """The component contract of the part's seams; None for its browser tests."""
        return None if self.browser else self.language.component_contract

    @property
    def browser_contract(self) -> BrowserContract | None:
        """The page contract of a part's browser tests."""
        if not self.browser or not isinstance(self.language, PartLanguage):
            return None
        return self.language.browser_contract

    def contract_dir(self) -> PurePosixPath:
        """Where the part's component contract (or page contract) lives, relative to the
        routing's directory (and to `contracts/` in the repository)."""
        if self.browser:
            return PurePosixPath(PARTS_DIR, self.name, BROWSER_DIR)
        return (PurePosixPath(COMPONENTS_DIR) if self.is_service
                else PurePosixPath(PARTS_DIR, self.name, COMPONENTS_DIR))

    def operations_file(self, session_dir: Path) -> Path:
        """Where the routing keeps the API's operations beside a part's page contract:
        how the runners name the page's requests."""
        from app.routing.browser import OPERATIONS_FILE
        return session_dir / 'routing' / PARTS_DIR / self.name / OPERATIONS_FILE

    def routed_contract(self, session_dir: Path) -> Path:
        """Where the routing kept the part's component contract, checked against the code,
        in the session's artifacts: the stubs every later stage is given."""
        return session_dir / 'routing' / self.contract_dir()

    def workspace_contract(self) -> PurePosixPath:
        """Where an agent's workspace holds the part's stubs, below its contracts."""
        return PurePosixPath('contracts') / self.contract_dir()

    def published_dir(self) -> PurePosixPath:
        """Where the part's component contract is published in the repository."""
        return PurePosixPath(CONTRACTS_DIR) / self.contract_dir()

    def describe(self) -> dict[str, str]:
        """What the routing agent is told of the part."""
        return {'name': self.name, 'root': self.root, 'language': self.language.label}

    def record(self) -> dict[str, str]:
        """How the routing records the part in its routes, for the stages after it."""
        return {'name': self.name, 'root': self.root, 'language': self.language.name,
                **({'kind': BROWSER_KIND} if self.browser else {})}

    def tests_dir(self) -> PurePosixPath:
        """Where the part's component tests, or browser tests, live in the repository."""
        if self.browser:
            return PurePosixPath(self.root, BROWSER_TESTS_DIR)
        return (PurePosixPath(COMPONENT_TESTS_DIR) if self.is_service
                else PurePosixPath(self.root, COMPONENT_TESTS_DIR))

    def snapshot_dir(self) -> PurePosixPath:
        """Where the record of the repository's tests holds the part's, within that of the
        component tests: the part's place in a delivery's suite."""
        if self.browser:
            return PurePosixPath(BROWSER_MEMBER, self.name)
        return PurePosixPath('.') if self.is_service else PurePosixPath(SUITE_PARTS_DIR, self.name)

    @property
    def suite(self) -> ComponentSuite | None:
        if self.browser:
            return self.language.browser_suite if isinstance(self.language, PartLanguage) else None
        return self.language.component_suite


def component_parts(language: Language, checkout: Path) -> list[ComponentPart]:
    """The service, then every other project of the checkout that a registered part
    language detects, by its directory. A project inside another such part or inside the
    service's sources is left out, as is one without a part language."""
    # Imported here: the implementation's activities read the routed parts.
    from app.implementation.activities import is_service_file
    parts = [ComponentPart(SERVICE_PART, '.', language)]
    sources = set(language.implementation.contract_paths)
    found = repository_parts(checkout, partial(is_service_file, language))
    for item in found:
        path = PurePosixPath(item['path'])
        if (item['path'] in ('.', SERVICE_PART) or path.parts[0] in sources
                or any(path.is_relative_to(part.root) for part in parts[1:])):
            continue
        detected = next((candidate for candidate in part_languages()
                         if candidate.detect(checkout / path)), None)
        if detected is None:
            continue
        parts.append(ComponentPart(path.as_posix(), path.as_posix(), detected))
        if (detected.detect_browser is not None and detected.browser_contract is not None
                and detected.browser_suite is not None
                and detected.detect_browser(checkout / path)):
            # Its web client: the browser route's pages.
            parts.append(ComponentPart(path.as_posix(), path.as_posix(), detected, browser=True))
    return parts


# The key of the routing's routes file that names the parts with `component` routes.
PARTS_KEY = 'parts'


def routed_parts(language: Language, routes: Any) -> list[ComponentPart]:
    """The parts with `component` routes, as the routing recorded them in its routes file
    (`routes`, its parsed object), the service first. A routing recorded before parts
    other than the service could be routed names none: its component routes are the
    service's."""
    items = routes.get(PARTS_KEY) if isinstance(routes, dict) else None
    if items is None:
        return [ComponentPart(SERVICE_PART, '.', language)]
    if not isinstance(items, list):
        raise ValueError('The routing names its parts other than as a list')
    parts: list[ComponentPart] = []
    for item in items:
        if not isinstance(item, dict) or not all(
                isinstance(item.get(key), str) for key in ('name', 'root', 'language')):
            raise ValueError('The routing names a part without its name, root and language')
        if item['name'] == SERVICE_PART:
            parts.append(ComponentPart(SERVICE_PART, '.', language))
            continue
        path = PurePosixPath(item['root'])
        if (item['name'] != item['root'] or path.is_absolute() or '..' in path.parts
                or path.as_posix() != item['root']):
            raise ValueError(f'The routing names an invalid part: {item["name"][:80]}')
        try:
            part_language = get_part_language(item['language'])
        except UnsupportedLanguage as exc:
            raise ValueError(str(exc)) from exc
        kind = item.get('kind')
        if kind not in (None, BROWSER_KIND):
            raise ValueError(f'The routing names a part of an unknown kind: {str(kind)[:40]}')
        browser = kind == BROWSER_KIND
        if browser and part_language.browser_suite is None:
            raise ValueError(f'{part_language.label} has no browser tests')
        parts.append(ComponentPart(item['name'], item['root'], part_language, browser=browser))
    if len({part.key for part in parts}) != len(parts):
        raise ValueError('The routing names a part twice')
    return sorted(parts, key=lambda part: not part.is_service)


def read_routed_parts(language: Language, session_dir: Path) -> list[ComponentPart]:
    """The parts with `component` routes of the session's routing."""
    from app.routing.agent import ROUTES
    file = session_dir / 'routing' / ROUTES
    try:
        routes = json.loads(file.read_text(encoding='utf-8'))
    except FileNotFoundError:
        routes = None
    except ValueError as exc:
        raise ValueError('The routing\'s routes are unreadable') from exc
    return routed_parts(language, routes)


def component_suite(language: Language, parts: list[ComponentPart]) -> ComponentSuite:
    """The component suite of a delivery whose `component` routes are at seams of
    `parts`: the service's own suite when it is the only one, as before parts other than
    the service could be routed, and otherwise one project per part
    (app/languages/composite.py). Raises ValueError for a part without component tests."""
    suites = []
    for part in parts:
        if part.suite is None:
            raise ValueError(f'{part.language.label} has no '
                             f'{"browser" if part.browser else "component"} tests')
        suites.append(part.suite)
    if [part.key for part in parts] == [SERVICE_PART]:
        return suites[0]
    return composite_suite([SuiteMember(part.key, suite, service=part.is_service,
                                        label=part.language.label, browser=part.browser)
                            for part, suite in zip(parts, suites)])


def read_operations(part: ComponentPart, session_dir: Path) -> list[Any]:
    """The API's operations the routing kept beside a part's page contract."""
    try:
        operations = json.loads(part.operations_file(session_dir).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'The API operations of the page contract of part {part.name} are '
                         'unavailable') from exc
    if not isinstance(operations, list):
        raise ValueError(f'The API operations of the page contract of part {part.name} are '
                         'unreadable')
    return operations


async def _check_pages(part: ComponentPart, session_dir: Path, code: Path, work: Path,
                       log_dir: Path) -> None:
    from app.routing.browser import check_page_conformance
    contract = part.browser_contract
    assert contract is not None  # only a part with browser tests is routed to a page
    operations = await run_sync(read_operations, part, session_dir)
    await check_page_conformance(contract, code, part.root, part.routed_contract(session_dir),
                                 operations, [], work, log_dir)


async def check_parts(parts: list[ComponentPart], session_dir: Path, code: Path, work: Path,
                      log_dir: Path) -> str | None:
    """Check the component contract, or page contract, of each part with routes against
    `code`, the repository's sources: None when every one holds, or what the first that
    does not says, naming its part when it is not the service. Every element of a page
    contract is expected: those the change adds are added by now."""
    for part in parts:
        if part.browser:
            own = work / 'checks' / part.key
            await run_sync(own.mkdir, parents=True)
            logs = log_dir / PARTS_DIR / part.name / BROWSER_DIR
            await run_sync(logs.mkdir, parents=True, exist_ok=True)
            try:
                await _check_pages(part, session_dir, code, own, logs)
            except (RuntimeError, ValueError) as exc:
                return f'Part {part.label}: {exc}'
            continue
        contract = part.contract
        assert contract is not None  # only a part with component tests is routed to
        components = part.routed_contract(session_dir)
        modules = await run_sync(stub_modules, contract, components)
        own = work / 'checks' / part.name
        await run_sync(own.mkdir, parents=True)
        logs = log_dir if part.is_service else log_dir / PARTS_DIR / part.name
        await run_sync(logs.mkdir, parents=True, exist_ok=True)
        try:
            await check_conformance(contract, code, components, modules, own, logs)
        except RuntimeError as exc:
            return str(exc) if part.is_service else f'Part {part.name}: {exc}'
    return None
