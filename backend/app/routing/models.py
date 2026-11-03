"""Which test suite can prove each criterion of a change, decided before any test is
written: a criterion no suite can observe stops the delivery while that is still cheap."""
from typing import Any, Literal, NotRequired, TypedDict

# `contract`: observable over HTTP against the service as verification builds and runs
# it. `component`: observable only at a seam of the code, an interface it already has,
# described by the stubs of the component contract. `unobservable`: no suite of the
# delivery can observe it.
Route = Literal['contract', 'component', 'unobservable']
ROUTES = ('contract', 'component', 'unobservable')
# Who decided the route: the routing stage, or the test author, who may find a criterion
# routed to the contract tests unobservable after all.
RouteSource = Literal['routing', 'tests']
# `routed`: every criterion has a suite that observes it. `unobservable`: one has none,
# and the delivery stopped before its tests were run. `unsupported`: a criterion can only
# be proved by component tests, which no delivery writes yet, and the delivery stopped.
RoutingStatus = Literal['not_applicable', 'waiting', 'running', 'routed', 'unobservable',
                        'unsupported', 'error']
REASON_CHARS = 800
NAMED_CRITERIA = 5
MAX_SEAM_SYMBOLS = 50
SYMBOL_CHARS = 200
# The file in which the test author names criteria it cannot test, instead of a suite.
UNOBSERVABLE_FILE = 'unobservable.json'


class Seam(TypedDict):
    # The dotted module the stubs describe, and the symbols of it, by dotted name within
    # the module (`Class` or `Class.method`), at which the criterion is observed.
    module: str
    symbols: list[str]


class RequirementRoute(TypedDict):
    route: Route
    # How the suite observes the criterion, or why none can.
    reason: str
    source: RouteSource
    # Only for the `component` route.
    seam: NotRequired[Seam]


def read_seam(requirement: str, seam: Any) -> Seam:
    """A `component` route's untrusted seam: a module and the symbols of it it names."""
    if not isinstance(seam, dict) or not isinstance(seam.get('module'), str):
        raise RuntimeError(f'The component route of requirement {requirement} names no seam '
                           'module')
    symbols = seam.get('symbols')
    if (not isinstance(symbols, list) or not symbols or len(symbols) > MAX_SEAM_SYMBOLS
            or not all(isinstance(item, str) and 0 < len(item.strip()) <= SYMBOL_CHARS
                       for item in symbols)):
        raise RuntimeError(f'The seam of requirement {requirement} names no symbols, or more '
                           f'than {MAX_SEAM_SYMBOLS}')
    return {'module': seam['module'].strip()[:SYMBOL_CHARS],
            'symbols': list(dict.fromkeys(item.strip() for item in symbols))}


def read_routes(data: Any, requirement_ids: list[str],
                routes: tuple[str, ...] = ROUTES) -> dict[str, dict[str, Any]]:
    """An agent's untrusted routing as {requirement ID: {route, reason, seam?}}: one entry
    with a reason for each of `requirement_ids`, no other, each with one of `routes`, and
    a `component` route with the seam it names."""
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
            result[requirement]['seam'] = read_seam(requirement, item.get('seam'))
    missing = [requirement for requirement in requirement_ids if requirement not in result]
    if missing:
        raise RuntimeError(f'Requirements without a route: {", ".join(missing)}')
    return result


def judge_routes(routes: dict[str, RequirementRoute]) -> tuple[RoutingStatus, str | None]:
    """Whether every criterion has a suite that can prove it, and what to tell the user
    when one has none."""
    unobservable = [(requirement, item) for requirement, item in routes.items()
                    if item['route'] == 'unobservable']
    component = [(requirement, item) for requirement, item in routes.items()
                 if item['route'] == 'component']
    if not unobservable and component:
        # Steps 3 and 4 of plans/component-contracts.md: until component tests are
        # written and run, nothing could prove these criteria red to green.
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
        ('The test author found that the contract tests cannot observe '
         if by_tests else 'The contract tests cannot observe ')
        + f'{len(unobservable)} of the {len(routes)} acceptance criteria, so nothing was '
        f'changed: {named}{f"; {more} more" if more > 0 else ""}. Rephrase each of them as '
        "behaviour the service's HTTP API shows, in a fork of the interview.")
