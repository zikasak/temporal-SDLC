"""Which test suite can prove each criterion of a change, decided before any test is
written: a criterion no suite can observe stops the delivery while that is still cheap."""
from typing import Any, Literal, NotRequired, TypedDict

# `contract`: observable over HTTP against the service as verification builds and runs
# it. `component`: observable only at a seam of the code, an interface it already has or
# one the change adds first without changing behaviour (the seam's `added` symbols),
# described by the stubs of the component contract. `unobservable`: no suite of the
# delivery can observe it.
Route = Literal['contract', 'component', 'unobservable']
ROUTES = ('contract', 'component', 'unobservable')
# Who decided the route: the routing stage, or the test author, who may find a criterion
# routed to the contract tests unobservable after all.
RouteSource = Literal['routing', 'tests']
# `routed`: every criterion has a suite that observes it. `unobservable`: one has none,
# and the delivery stopped before its tests were run. `unsupported`: a criterion can only
# be proved by component tests, which deliveries started before they could write them do
# not write, and the delivery stopped.
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


class RequirementRoute(TypedDict):
    route: Route
    # How the suite observes the criterion, or why none can.
    reason: str
    source: RouteSource
    # Only for the `component` route.
    seam: NotRequired[Seam]


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


def _names(value: Any) -> bool:
    return (isinstance(value, list) and 0 < len(value) <= MAX_SEAM_SYMBOLS
            and all(isinstance(item, str) and 0 < len(item.strip()) <= SYMBOL_CHARS
                    for item in value))


def seam_additions(seam: Seam) -> dict[str, list[str]]:
    """The symbols a seam adds first, by module of its part: those of its own module and
    of the other modules it names."""
    additions = {seam['module']: list(seam['added'])} if seam.get('added') else {}
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
    have yet, in its own module or the others it names."""
    added: dict[str, list[str]] = {}
    for route in routes.values():
        seam = route.get('seam') if route.get('route') == 'component' else None
        if seam is None:
            continue
        for module, additions in seam_additions(seam).items():
            key = seam_key({**seam, 'module': module})
            symbols = added.setdefault(key, [])
            for symbol in additions:
                if symbol not in symbols:
                    symbols.append(symbol)
    return added


def read_routes(data: Any, requirement_ids: list[str], routes: tuple[str, ...] = ROUTES,
                parts: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    """An agent's untrusted routing as {requirement ID: {route, reason, seam?}}: one entry
    with a reason for each of `requirement_ids`, no other, each with one of `routes`, and
    a `component` route with the seam it names, in the service or one of `parts`."""
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
        if item['route'] == 'component':
            result[requirement]['seam'] = read_seam(requirement, item.get('seam'), parts)
    missing = [requirement for requirement in requirement_ids if requirement not in result]
    if missing:
        raise RuntimeError(f'Requirements without a route: {", ".join(missing)}')
    return result


# Why a change whose component tests were written stops before its baseline: step 4 of
# plans/component-contracts.md, which runs them, is still to come.
COMPONENT_TESTS_UNRUN = (
    'The component tests of the acceptance criteria routed to seams of the code were written '
    'against their component contract and built with the code, but deliveries cannot run '
    'component tests yet, so nothing could show them failing without the fix and nothing was '
    'changed. Rephrase those criteria as behaviour the service\'s HTTP API shows, in a fork '
    'of the interview, if the change cannot wait.')


def judge_routes(routes: dict[str, RequirementRoute],
                 component_tests: bool = True) -> tuple[RoutingStatus, str | None]:
    """Whether every criterion has a suite that can prove it, and what to tell the user
    when one has none. `component_tests` is false for a delivery started before deliveries
    wrote component tests, which a criterion routed to a seam stops."""
    unobservable = [(requirement, item) for requirement, item in routes.items()
                    if item['route'] == 'unobservable']
    component = [(requirement, item) for requirement, item in routes.items()
                 if item['route'] == 'component']
    if not unobservable and component and not component_tests:
        # Kept for the histories of such deliveries, which have to replay as they ran.
        named = '; '.join(f'{requirement} at {item["seam"]["module"]}'
                          for requirement, item in component[:NAMED_CRITERIA])
        more = len(component) - NAMED_CRITERIA
        return 'unsupported', (
            f'{len(component)} of the {len(routes)} acceptance criteria can only be observed '
            f'through the code, not over HTTP ({named}{f"; {more} more" if more > 0 else ""}), '
            'and deliveries cannot write component tests yet, so nothing was changed. Their '
            'component contract was checked against the code and is kept with the routing. '
            "Rephrase them as behaviour the service's HTTP API shows, in a fork of the "
            'interview, if the change cannot wait.')
    if not unobservable:
        return 'routed', None
    named = '; '.join(f'{requirement}: {item["reason"]}'
                      for requirement, item in unobservable[:NAMED_CRITERIA])
    more = len(unobservable) - NAMED_CRITERIA
    by_tests = all(item['source'] == 'tests' for _, item in unobservable)
    return 'unobservable', (
        ('The test author found that its suite cannot observe '
         if by_tests else 'The contract tests cannot observe ')
        + f'{len(unobservable)} of the {len(routes)} acceptance criteria, so nothing was '
        f'changed: {named}{f"; {more} more" if more > 0 else ""}. Rephrase each of them as '
        "behaviour the service's HTTP API shows, in a fork of the interview.")
