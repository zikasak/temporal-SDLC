"""Which test suite can prove each criterion of a change, decided before any test is
written: a criterion no suite can observe stops the delivery while that is still cheap."""
import json
import re
from pathlib import PurePosixPath
from typing import Any, Literal, NotRequired, TypedDict, cast

# `contract`: observable over HTTP against the service as verification builds and runs
# it. `component`: observable only at a seam of the code, an interface it already has or
# one the change adds first without changing behaviour (the seam's `added` symbols),
# described by the stubs of the component contract. `browser`: observable only on a page
# of a part's built web client in a real browser, where layout, painting, stacking, focus
# or input events decide it, described by the part's page contract. `unobservable`: no
# suite of the delivery can observe it.
Route = Literal['contract', 'component', 'browser', 'unobservable']
ROUTES = ('contract', 'component', 'browser', 'unobservable')
# Who decided the route: the routing stage, or the test author, who may find a criterion
# routed to the contract tests unobservable after all.
RouteSource = Literal['routing', 'tests']
# `routed`: every criterion has a suite that observes it. `unobservable`: one has none,
# and the delivery stopped before its tests were run.
# `unsupported`: a delivery started before the browser tests existed routed a criterion
# to them, and stopped (the `browser-tests` workflow patch).
RoutingStatus = Literal['not_applicable', 'waiting', 'running', 'routed', 'unobservable',
                        'unsupported', 'error']
REASON_CHARS = 800
NAMED_CRITERIA = 5
MAX_SEAM_SYMBOLS = 50
SYMBOL_CHARS = 200
# The file in which the test author names criteria it cannot test, instead of a suite.
UNOBSERVABLE_FILE = 'unobservable.json'
# How a seam's error begins when the code with the seam added does not match the component
# contract the routing wrote. The contract may be what is wrong, such as a stub of a symbol
# the code lacks that no seam adds, so its retry routes the criteria again.
SEAM_MISMATCH = ('The code with the seam the change added does not match its component '
                 'contract, so no test was written:')
# The type of the routing's own failures: why the routes or the component contract were
# unusable, or what the routing lacked. Their messages carry no provider output, so the
# user is shown them, while failures of the agent itself are not (NFR-2).
ROUTING_ERROR = 'RoutingError'
ROUTING_ERROR_CHARS = 800
# The type of the failures of adding a seam that say why in words of the worker's own:
# which step failed (the agent, its output, the image build, the contract check), never
# what the agent or its provider said, so the user is shown them (NFR-2).
SEAM_ERROR = 'SeamError'


class Seam(TypedDict):
    # The part of the repository the seam is in, by its directory, such as `frontend`;
    # absent for the service.
    part: NotRequired[str]
    # The module the stubs describe (dotted for the service's languages, a path within
    # the part for a TypeScript part), and the symbols of it, by dotted name within the
    # module (`Class` or `Class.method`), at which the criterion is observed.
    module: str
    symbols: list[str]
    # Symbols of the module's stub the code does not have yet: the change adds them, with
    # no behaviour change, before the criterion is shown failing through them. Absent
    # when the seam exists as the repository holds it.
    added: NotRequired[list[str]]
    # Symbols the change adds first, as `added`, to other modules of the seam's part by
    # module (named as `module` is): the collaborators the seam's stub names that the
    # code lacks too, such as an activity the tests fake or a constant they read.
    added_elsewhere: NotRequired[dict[str, list[str]]]


class Element(TypedDict, total=False):
    # How a test finds an element of a page: an ARIA role with its accessible name, if
    # any, or a data-testid.
    role: str
    name: str
    testid: str


class Page(TypedDict):
    # The part of the repository whose web client shows the page, by its directory.
    part: str
    # The path within the client the test opens, such as `/` or `/requirements/1`.
    entry: str
    # The names of the page contract's viewports the criterion must hold in.
    viewports: list[str]
    # The elements the test finds.
    roles: list[Element]
    # Those of `roles` the page lacks yet: the change adds them first, with no other
    # change, as it adds a seam's symbols.
    added: NotRequired[list[Element]]


class RequirementRoute(TypedDict):
    route: Route
    # How the suite observes the criterion, or why none can.
    reason: str
    source: RouteSource
    # For the `component` route, and for one the component tests' author found
    # unobservable at that seam after all: the seam it was routed to.
    seam: NotRequired[Seam]
    # For the `browser` route: the page it was routed to.
    page: NotRequired[Page]
    # For an `unobservable` route declined by the test author: whether the author hit
    # a missing/underspecified stub or seam (`contract_gap`) or genuine unobservability.
    kind: NotRequired[Literal['contract_gap', 'unobservable']]
    # For an `unobservable` route of the routing: the criterion rephrased as behaviour a
    # suite observes, and the other criteria that already prove all of it that any suite
    # could, so it can be removed. Either is absent when the routing found none.
    suggestion: NotRequired[str]
    covered_by: NotRequired[list[str]]


def read_seam(requirement: str, seam: Any, parts: tuple[str, ...] = ()) -> Seam:
    """A `component` route's untrusted seam: the part it is in, one of `parts` (the
    repository's parts other than the service) or none for the service, and a module and
    the symbols of it it names."""
    if not isinstance(seam, dict) or not isinstance(seam.get('module'), str):
        raise RuntimeError(f'The component route of requirement {requirement} names no seam '
                           'module')
    part = seam.get('part')
    if part is not None and (not isinstance(part, str) or part.strip() not in parts):
        named = ', '.join(parts) or 'none but the service'
        raise RuntimeError(f'The seam of requirement {requirement} names a part the repository '
                           f'does not have: {str(part)[:80]}; its other parts are {named}')
    symbols = seam.get('symbols')
    if (not isinstance(symbols, list) or not symbols or len(symbols) > MAX_SEAM_SYMBOLS
            or not all(isinstance(item, str) and 0 < len(item.strip()) <= SYMBOL_CHARS
                       for item in symbols)):
        raise RuntimeError(f'The seam of requirement {requirement} names no symbols, or more '
                           f'than {MAX_SEAM_SYMBOLS}')
    read: Seam = {'module': seam['module'].strip()[:SYMBOL_CHARS],
                  'symbols': list(dict.fromkeys(item.strip() for item in symbols))}
    if part is not None:
        read = {'part': part.strip(), **read}
    added = seam.get('added')
    if added is not None and added != []:
        if not _names(added):
            raise RuntimeError(f'The seam of requirement {requirement} names the symbols it '
                               f'adds other than as a list of at most {MAX_SEAM_SYMBOLS} names')
        read['added'] = list(dict.fromkeys(item.strip() for item in added))
    elsewhere = seam.get('added_elsewhere')
    if elsewhere is None or elsewhere == {}:
        return read
    if (not isinstance(elsewhere, dict) or len(elsewhere) > MAX_SEAM_SYMBOLS
            or not all(isinstance(module, str) and 0 < len(module.strip()) <= SYMBOL_CHARS
                       and _names(symbols) for module, symbols in elsewhere.items())):
        raise RuntimeError(f'The seam of requirement {requirement} names the symbols it adds '
                           'to other modules other than as an object of modules, each with a '
                           f'list of at most {MAX_SEAM_SYMBOLS} names')
    others: dict[str, list[str]] = {}
    for module, symbols in elsewhere.items():
        named = others.setdefault(module.strip(), [])
        named += [item for item in dict.fromkeys(item.strip() for item in symbols)
                  if item not in named]
    if read['module'] in others:
        raise RuntimeError(f'The seam of requirement {requirement} names symbols of its own '
                           'module in `added_elsewhere`; they belong in `added`')
    if sum(len(symbols) for symbols in others.values()) > MAX_SEAM_SYMBOLS:
        raise RuntimeError(f'The seam of requirement {requirement} adds more than '
                           f'{MAX_SEAM_SYMBOLS} symbols to other modules')
    read['added_elsewhere'] = others
    return read


MAX_PAGE_ELEMENTS = 30
MAX_PAGE_VIEWPORTS = 8
ENTRY = re.compile(r'/[A-Za-z0-9._~!$&\'()*+,;=:@%/?#-]{0,199}')
ROLE = re.compile(r'[a-z]{2,40}')
TESTID = re.compile(r'[A-Za-z0-9_.:-]{1,100}')
# A viewport's name: lowercase words, as viewports.json and the browser tests' tags give it.
VIEWPORT = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*')
ELEMENT_NAME_CHARS = 200


class PageContractError(RuntimeError):
    """Why a page contract, or a route's page, is unusable."""


def element_key(element: Element | dict[str, str]) -> str:
    """How an element is named in messages, added seams and the conformance check, as
    check-pages.mjs names it: `role=button[name="FR-1"]`, `role=tooltip` or
    `testid=popup`."""
    if 'testid' in element:
        return f'testid={element.get("testid")}'
    if 'name' in element:
        return (f'role={element.get("role")}'
                f'[name={json.dumps(element.get("name"), ensure_ascii=False)}]')
    return f'role={element.get("role")}'


def read_element(value: Any, where: str) -> Element:
    """An untrusted element: an ARIA role with an optional accessible name, or a
    data-testid."""
    if not isinstance(value, dict):
        raise PageContractError(f'{where} is not an element')
    if set(value) == {'testid'}:
        if not isinstance(value['testid'], str) or not TESTID.fullmatch(value['testid']):
            raise PageContractError(f'{where} has an invalid testid')
        return {'testid': value['testid']}
    if set(value) in ({'role'}, {'role', 'name'}):
        if not isinstance(value['role'], str) or not ROLE.fullmatch(value['role']):
            raise PageContractError(f'{where} has an invalid ARIA role')
        element: Element = {'role': value['role']}
        if 'name' in value:
            name = value['name']
            if (not isinstance(name, str) or not name.strip()
                    or len(name) > ELEMENT_NAME_CHARS):
                raise PageContractError(f'{where} has an invalid accessible name')
            element['name'] = name
        return element
    raise PageContractError(f'{where} is neither {{"role", "name"?}} nor {{"testid"}}')


def read_entry(value: Any, where: str) -> str:
    """A path within a web client, as a page's `entry`."""
    if (not isinstance(value, str) or not ENTRY.fullmatch(value)
            or '..' in PurePosixPath(value.split('?')[0].split('#')[0]).parts):
        raise PageContractError(f'{where} has no entry: a path within the client starting '
                                'with /')
    return value


def read_page(requirement: str, page: Any, parts: tuple[str, ...] = ()) -> Page:
    """A `browser` route's untrusted page: one of `parts` (the repository's parts whose
    language has browser tests), an entry within the client, the viewports and the
    elements the test uses, and those of them the change adds."""
    if not isinstance(page, dict):
        raise RuntimeError(f'The browser route of requirement {requirement} names no page')
    part = page.get('part')
    if not isinstance(part, str) or part.strip() not in parts:
        named = ', '.join(parts) or 'none'
        raise RuntimeError(f'The page of requirement {requirement} names no part with browser '
                           f'tests: {str(part)[:80]}; the parts with them are {named}')
    try:
        entry = read_entry(page.get('entry'), f'The page of requirement {requirement}')
        viewports = page.get('viewports')
        if (not isinstance(viewports, list) or not viewports
                or len(viewports) > MAX_PAGE_VIEWPORTS
                or not all(isinstance(item, str) and VIEWPORT.fullmatch(item)
                           for item in viewports)):
            raise PageContractError(f'The page of requirement {requirement} names no viewports '
                                    f'as a list of 1 to {MAX_PAGE_VIEWPORTS} names')
        roles = page.get('roles')
        if not isinstance(roles, list) or not roles or len(roles) > MAX_PAGE_ELEMENTS:
            raise PageContractError(f'The page of requirement {requirement} names no roles as a '
                                    f'list of 1 to {MAX_PAGE_ELEMENTS} elements')
        elements = [read_element(item, f'A role of the page of requirement {requirement}')
                    for item in roles]
        added = page.get('added') or []
        if not isinstance(added, list) or len(added) > MAX_PAGE_ELEMENTS:
            raise PageContractError(f'The page of requirement {requirement} names the elements '
                                    'it adds other than as a list')
        additions = [read_element(item, f'An added element of the page of requirement '
                                        f'{requirement}') for item in added]
    except PageContractError as exc:
        raise RuntimeError(str(exc)) from exc
    keys = {element_key(item) for item in elements}
    extra = [element_key(item) for item in additions if element_key(item) not in keys]
    if extra:
        raise RuntimeError(f'The page of requirement {requirement} adds elements its roles do '
                           f'not name: {", ".join(extra)[:400]}')
    read: Page = {'part': part.strip(), 'entry': entry,
                  'viewports': list(dict.fromkeys(viewports)),
                  'roles': list({element_key(item): item for item in elements}.values())}
    if additions:
        read['added'] = list({element_key(item): item for item in additions}.values())
    return read


def page_key(page: Page) -> str:
    """How the elements a page adds are named among the seams' modules: after the part,
    as `frontend/page:/requirements`."""
    return f'{page["part"]}/page:{page["entry"]}'


def _names(value: Any) -> bool:
    return (isinstance(value, list) and 0 < len(value) <= MAX_SEAM_SYMBOLS
            and all(isinstance(item, str) and 0 < len(item.strip()) <= SYMBOL_CHARS
                    for item in value))


def seam_additions(seam: Seam) -> dict[str, list[str]]:
    """The symbols a seam adds first, by module of its part: those of its own module and
    of the other modules it names."""
    added = seam.get('added')
    additions = {seam['module']: list(added)} if added else {}
    for module, symbols in (seam.get('added_elsewhere') or {}).items():
        additions.setdefault(module, []).extend(symbols)
    return additions


def seam_key(seam: Seam) -> str:
    """How a seam's module is named among those of every part: the service's by itself,
    as before other parts could be routed, and another part's after the part, as the path
    of the module in the repository (`frontend/src/Flows`)."""
    part = seam.get('part')
    return seam['module'] if part is None else f'{part}/{seam["module"]}'


def added_seams(routes: dict[str, Any]) -> dict[str, list[str]]:
    """The symbols a change adds before it reproduces anything, by part and module (as
    `seam_key` names them): those of every `component` route whose seam the code does not
    have yet, in its own module or the others it names, and the elements of every
    `browser` route's page that the client lacks yet, by `page_key`, each as
    `role=…[name=…]` or `testid=…`."""
    added: dict[str, list[str]] = {}
    for route in routes.values():
        page = route.get('page') if route.get('route') == 'browser' else None
        if page is not None and page.get('added'):
            elements = added.setdefault(page_key(page), [])
            for element in page['added']:
                if element_key(element) not in elements:
                    elements.append(element_key(element))
        seam = route.get('seam') if route.get('route') == 'component' else None
        if seam is None:
            continue
        for module, additions in seam_additions(seam).items():
            key = seam_key(cast(Seam, {**seam, 'module': module}))
            symbols = added.setdefault(key, [])
            for symbol in additions:
                if symbol not in symbols:
                    symbols.append(symbol)
    return added


def read_routes(data: Any, requirement_ids: list[str], routes: tuple[str, ...] = ROUTES,
                parts: tuple[str, ...] = (), browser_parts: tuple[str, ...] = ()
                ) -> dict[str, dict[str, Any]]:
    """An agent's untrusted routing as {requirement ID: {route, reason, seam?, page?}}:
    one entry with a reason for each of `requirement_ids`, no other, each with one of
    `routes`, a `component` route with the seam it names, in the service or one of
    `parts`, and a `browser` route with the page it names, of one of `browser_parts`."""
    items = data.get('routes') if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise RuntimeError('The routing is not a list of routes')
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('requirement_id'), str):
            raise RuntimeError('A route does not name its requirement')
        requirement = item['requirement_id'].strip()
        if requirement not in requirement_ids:
            raise RuntimeError(f'A route names a requirement the brief does not have: '
                               f'{requirement[:80]}')
        if requirement in result:
            raise RuntimeError(f'Requirement {requirement} is routed twice')
        if item.get('route') not in routes:
            raise RuntimeError(f'Requirement {requirement} has no valid route; the routes '
                               f'are {", ".join(routes)}')
        reason = ' '.join(str(item.get('reason') or '').split())
        if not reason:
            raise RuntimeError(f'The route of requirement {requirement} gives no reason')
        result[requirement] = {'route': item['route'], 'reason': reason[:REASON_CHARS]}
        if item['route'] == 'unobservable':
            result[requirement].update(_rephrasing(requirement, item, requirement_ids))
        elif item['route'] == 'component':
            result[requirement]['seam'] = read_seam(requirement, item.get('seam'), parts)
        elif item['route'] == 'browser':
            result[requirement]['page'] = read_page(requirement, item.get('page'),
                                                    browser_parts)
    missing = [requirement for requirement in requirement_ids if requirement not in result]
    if missing:
        raise RuntimeError(f'Requirements without a route: {", ".join(missing)}')
    for requirement, item in result.items():
        unproved = [other for other in item.get('covered_by', [])
                    if result[other]['route'] == 'unobservable']
        if unproved:
            raise RuntimeError(f'Requirement {requirement} is covered by requirements no suite '
                               f'observes either: {", ".join(unproved)}')
    return result


def _rephrasing(requirement: str, item: dict[str, Any], requirement_ids: list[str]
                ) -> dict[str, Any]:
    """An `unobservable` route's untrusted `suggestion` and `covered_by`, each optional."""
    read: dict[str, Any] = {}
    suggestion = item.get('suggestion')
    if suggestion is not None and not isinstance(suggestion, str):
        raise RuntimeError(f'The suggestion of requirement {requirement} is not a text')
    if suggestion and ' '.join(suggestion.split()):
        read['suggestion'] = ' '.join(suggestion.split())[:REASON_CHARS]
    covered = item.get('covered_by')
    if covered is None or covered == []:
        return read
    if (not isinstance(covered, list)
            or not all(isinstance(other, str) and other.strip() in requirement_ids
                       and other.strip() != requirement for other in covered)):
        raise RuntimeError(f'Requirement {requirement} is covered by requirements other than '
                           'other requirements of the brief')
    read['covered_by'] = list(dict.fromkeys(other.strip() for other in covered))
    return read


# Words of an `unobservable` reason that say the criterion is about how a page looks or
# fits: the stop message then suggests the measurable form a browser test can decide.
LAYOUT_WORDS = re.compile(
    r'\b(layout|laid[- ]out|clip\w*|cut off|overflow\w*|scroll\w*|z-index|stack\w*|'
    r'overlap\w*|cover\w*|on top|viewport|window size|screen size|responsive|breakpoint|'
    r'css|styles?|visible|visibility|render\w*|paint\w*|position\w*|jsdom|hover|tap)\b',
    re.I)
MEASURABLE = ('Say which element, at which window sizes (such as 1280×800 by mouse or '
              '390×844 by tap), by which input, and what must hold: inside the window, on top '
              'and not cut off, or before or after another element; "looks right" is not a '
              'criterion.')


def _rephrased(item: RequirementRoute) -> str:
    """What the routing proposed for an unobservable criterion, as the stop message says."""
    covered = item.get('covered_by')
    if covered:
        return (f' {", ".join(covered)} already '
                f'{"proves" if len(covered) == 1 else "prove"} what can be observed of it, '
                'so it can be removed')
    suggestion = item.get('suggestion')
    return f' Proposed instead: "{suggestion}"' if suggestion else ''


def judge_routes(routes: dict[str, RequirementRoute], parts: bool = False
                 ) -> tuple[RoutingStatus, str | None]:
    """Whether every criterion has a suite that can prove it, and what to tell the user
    when one has none. `parts`: the repository has parts beside the service, such as a
    web client, whose seams or pages could show a criterion."""
    unobservable = [(requirement, item) for requirement, item in routes.items()
                    if item['route'] == 'unobservable']
    if not unobservable:
        return 'routed', None
    named = '; '.join(f'{requirement}: {item["reason"]}{_rephrased(item)}'
                      for requirement, item in unobservable[:NAMED_CRITERIA])
    more = len(unobservable) - NAMED_CRITERIA
    by_tests = all(item['source'] == 'tests' for _, item in unobservable)
    # A criterion the author declined at a seam may need no more than stubs that name
    # what it lacked, which a new routing is told; HTTP would not observe it either.
    at_seam = any('seam' in item or 'page' in item for _, item in unobservable)
    shows = ("the HTTP API, a seam of the code or a page of the web client shows" if parts
             else "the service's HTTP API shows")
    shows_at_seam = (shows if parts
                     else "the service's HTTP API or a seam of its code shows")
    layout = (f' For a criterion about how a page looks or fits: {MEASURABLE}'
              if any(LAYOUT_WORDS.search(item['reason']) for _, item in unobservable)
              else '')
    return 'unobservable', (
        ('The test author found that its suite cannot observe '
         if by_tests else 'The contract tests cannot observe ')
        + f'{len(unobservable)} of the {len(routes)} acceptance criteria, so nothing was '
        f'changed: {named}{f"; {more} more" if more > 0 else ""}. '
        + ('Retry the tests to route them again: the routing is told what the test author '
           'lacked, so it can write stubs or a page contract that name it. Otherwise '
           f'rephrase each of them as behaviour {shows_at_seam}, in a fork of the interview.'
           if at_seam else
           f'Rephrase each of them as behaviour {shows}, in a fork of the interview.')
        + layout)
