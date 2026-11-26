from typing import Any


def check_seams(routes: dict[str, dict[str, Any]], modules: dict[str, set[str]]) -> None:
    """Check the seams of the `component` routes against `modules`, the stubbed modules
    of the component contract by dotted name (such as 'app.docker_agents') with the
    symbols each stubs. `routes` maps requirement IDs to routes ({'route': 'component',
    'reason': str, 'seam': {'module': str, 'symbols': [str], 'added': [str] optional,
    'added_elsewhere': {module: [str]} optional}}); other routes are ignored.

    A seam module, or a module key of its added_elsewhere, written in path form
    ('app/docker_agents' or 'app/docker_agents.py') is normalized to dotted form
    ('app.docker_agents'); when the result is exactly one stubbed module, the route is
    updated IN PLACE with that dotted name (seam['module'], and the added_elsewhere key)
    and checking goes on as if it had been written so; nothing is raised for it.
    A module already written as a stubbed dotted name is left as is.

    A module that matches no stubbed module even after normalizing still raises
    RuntimeError, with the message unchanged: 'The seam of requirement <ID> names a module
    the component contract has no stub of: <module as written>' (for added_elsewhere:
    'The seam of requirement <ID> adds symbols to a module the component contract has no
    stub of: <module as written>'). Symbols not defined by the module's stub still raise
    RuntimeError 'The seam of requirement <ID> names symbols its stub does not define:
    ...'. Returns None."""
    ...
