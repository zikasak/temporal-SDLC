from pathlib import Path


def validate_completion(root: Path) -> None:
    """Check, while the routing agent still runs, the files it wrote under `root` (the
    agent's workspace: `routes.json` holding {"routes": [...]}, the service's component
    contract under `root/components/` as `.pyi` stubs at their module paths, such as
    `components/app/docker_agents.pyi` for `app.docker_agents`, and a part's under
    `root/parts/<part>/components/`). Raising RuntimeError sends its message back to the
    agent, which can then correct its routes or stubs; returning None accepts the
    routing.

    For every `component` route whose seam module, or module named in its
    added_elsewhere, cannot be matched to exactly one stubbed module of that seam's
    contract (the module as written, or after normalizing a path form such as
    'app/docker_agents' or 'app/docker_agents.py' to dotted form), it raises
    RuntimeError whose message names each such module as written and lists the modules
    the component contract stubs (dotted names). A module that is a path form of
    exactly one stubbed module does not raise. Other existing checks (readable
    routes.json, a seam with at least one stub, page contract files) are unchanged."""
    ...
