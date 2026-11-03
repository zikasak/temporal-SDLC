"""Which test suite can prove each criterion of a change, decided before any test is
written: a criterion no suite can observe stops the delivery while that is still cheap."""
from typing import Any, Literal, TypedDict

# `contract`: observable over HTTP against the service as verification builds and runs
# it. `unobservable`: no suite of the delivery can observe it.
Route = Literal['contract', 'unobservable']
ROUTES = ('contract', 'unobservable')
# Who decided the route: the routing stage, or the test author, who may find a criterion
# routed to the contract tests unobservable after all.
RouteSource = Literal['routing', 'tests']
# `routed`: every criterion has a suite that observes it. `unobservable`: one has none,
# and the delivery stopped before its tests were run.
RoutingStatus = Literal['not_applicable', 'waiting', 'running', 'routed', 'unobservable',
                        'error']
REASON_CHARS = 800
NAMED_CRITERIA = 5
# The file in which the test author names criteria it cannot test, instead of a suite.
UNOBSERVABLE_FILE = 'unobservable.json'


class RequirementRoute(TypedDict):
    route: Route
    # How the suite observes the criterion, or why none can.
    reason: str
    source: RouteSource


def read_routes(data: Any, requirement_ids: list[str],
                routes: tuple[str, ...] = ROUTES) -> dict[str, dict[str, str]]:
    """An agent's untrusted routing as {requirement ID: {route, reason}}: one entry with a
    reason for each of `requirement_ids`, no other, each with one of `routes`."""
    items = data.get('routes') if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise RuntimeError('The routing is not a list of routes')
    result: dict[str, dict[str, str]] = {}
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
    missing = [requirement for requirement in requirement_ids if requirement not in result]
    if missing:
        raise RuntimeError(f'Requirements without a route: {", ".join(missing)}')
    return result


def judge_routes(routes: dict[str, RequirementRoute]) -> tuple[RoutingStatus, str | None]:
    """Whether every criterion has a suite that can prove it, and what to tell the user
    when one has none."""
    unobservable = [(requirement, item) for requirement, item in routes.items()
                    if item['route'] == 'unobservable']
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
