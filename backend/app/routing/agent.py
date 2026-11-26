"""Container-only router. It decides which suite can prove each criterion; it writes no
test and changes no code."""
import asyncio
import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from app.asyncio_utils import run_sync
from app.agent_workflows.runtime import AgentProfile, configure_container_logging, run_agent

ROOT = Path('/workspace/routing')
# Present only when the contracts were too large to embed in input.json.
CONTRACTS = Path('/workspace/contracts')
ROUTES = 'routes.json'
# The component contract of the service, beside routes.json, and those of the other parts
# under PARTS/<part>/COMPONENTS.
COMPONENTS = 'components'
PARTS = 'parts'
# The page contract of a part, under PARTS/<part>/BROWSER.
BROWSER = 'browser'

# What the session's language calls itself and its tests, written by the host.
LANGUAGE = Path('/workspace/language.json')

PROMPT = """A change to an existing {label} service is about to be made red to green: an
independent test author, who never sees the implementation, first writes one regression
test per acceptance criterion, and every one of them has to fail against the service as it
is before anything is fixed. Your task is to decide, for every requirement in the
`brief` entry of the input, whether the project's HTTP contract test suite can observe it.
You write no test and change no file but routes.json{writes}.

/workspace/routing/repository is the project's Git repository as it is today, read-only
for your purposes. Its contract-tests/ directory holds the contract test suite and its
README.md; contracts/ holds the published contract. The suite runs like this, and nothing
else is available to it: the service image is built from the repository's root Dockerfile
and started alone, with no other container, credential or network service beside it unless
that Dockerfile itself provides it. The suite runs in its own container and reaches the
service only over HTTP at SERVICE_BASE_URL. It cannot read the service's files, logs or
database, call anything but the service's HTTP API, or wait for an outside event.

A requirement is `contract` when a test of that suite can bring the service, through its
public HTTP API alone, into the state the requirement is about and then observe the
required behaviour in HTTP responses. It is `unobservable` when that needs anything the
suite does not have, such as an outside system the service depends on (a code host, a
model provider, a workflow engine or a queue) that is not running beside it, an event only
such a system sends, or state the API cannot create.

An outside system's own state is never observable to any suite, but the service's side of
it often is. Read a requirement about what an outside system records or shows (a trace in
an observability tool, a comment on a code host, a message on a queue, a call a model
provider receives) as being about what the service sends that system, and one about what
the service does with such a system's answer as being about the service given that answer.
Route it by that reading, and route it `unobservable` only when even the service's side
of it needs what no suite has.

Decide from the code, not from the requirement's wording. For each requirement, find the
endpoints that would set it up and observe it, read how the service handles them, and read
the root Dockerfile and contract-tests/README.md to see what the running service has
available. Locate code with list_files and search_files and read only the relevant line
ranges with read_file. Do not run the service or the tests.

{component}
Finish by writing routes.json in /workspace/routing with write_file, containing exactly
this JSON object, with one entry for every requirement of the brief and no other:
{{"routes": [{{"requirement_id": "FR-…", "route": {routes}, "reason": "…"{seam}}}]}}
`requirement_id` is the requirement's `id` as the brief gives it. For `contract`, `reason`
names the requests that set up and observe the behaviour.{component_reason} For
`unobservable`, `reason` says in one or two sentences, for the user who wrote the
requirement, what the test would need that the suite does not have. Route a requirement
`unobservable` only when you are sure: the delivery then stops and the user is asked to
rephrase it. To spare them the guess, give such a route, when you can, a `suggestion`:
the requirement rephrased in one or two sentences, in the brief's words, as behaviour of
the same change that one of the routes you may choose observes, such as what the service
sends the outside system rather than what that system records. Give it `covered_by`, a
list of the IDs of other requirements of the brief that you did not route `unobservable`,
when those already observe everything of it any suite could, so it can simply be removed.
Leave out either when there is none. Only finish once routes.json is written.
"""

COMPONENT_PROMPT = """
A requirement the contract suite cannot observe may still be observable one level lower,
at a seam: an interface the code already has, as the repository holds it today, at which
an in-process test can bring the code into the state the requirement is about and observe
the behaviour, with everything the seam calls outside itself replaced by fakes. Such a test
is written by an author who never sees the code, only stubs of the seam you write. Good
seams already have contract-shaped interfaces: a workflow's signals, updates and queries,
which an in-process workflow test environment can drive with faked activities; a service
class whose collaborators are passed in; the code's own client, adapter or exporter of an
outside system, or the code that calls it, where a test fakes the outside system's library
or endpoint and observes what the code sends it (a tracer given an in-memory exporter, a
model client given a fake transport). A requirement is `component` when it is not
`contract` and such a seam exists; name it and write its stubs. Never invent a seam that
is not needed: a stubbed symbol must already exist with the stubbed signature, or the
check fails, unless you list it in the seam's `added`.

Prefer explicit-input seams over implicit-lookup wrappers: when the code already has a
function or method that takes its dependencies, workspace, checkout, or client directly as
arguments (such as `*_from(workspace, ...)` beside a wrapper taking only `session_id`),
route to the explicit-input seam so tests pass inputs directly instead of reconstructing
worker disk state or globals. Only when no explicit-input seam exists and an ID-based
wrapper reads hidden global state or directories (such as a session directory under a
configured root), either add a narrow explicit-input seam in `added` (calling it from the
wrapper with no behaviour change) when appropriate, or stub every lookup/config symbol it
uses and state the exact directory layout, environment variables, and failure preconditions
in the contract.

Only when no existing interface can observe the requirement, the change may add the
narrowest one that can, before anything is fixed: an entry point the code lacks, such as
a workflow's signal, update or query, or a method of an existing class, through which the
outside event the requirement is about reaches the code or its outcome is read. List
those symbols in `added` and stub them like the others, with the signature the code will
have. They are first added with no behaviour of their own and the existing tests run
unchanged against the code with them, so name only symbols that can exist without
changing what the code does today: never change, rename or remove an existing symbol, and
put the expected behaviour in their docstrings, not in their existence. When the tests
need a collaborator of the seam that the code lacks too, in another module of the same
part, such as a new activity the seam will call and the tests fake, or a constant they
read, stub it in its module and list it in the seam's `added_elsewhere` under that module;
it is added first the same way. Everything else the stubs name must already exist: a
symbol of a stub that the code lacks and no `added` or `added_elsewhere` lists fails the
check of the code with the seam, and no test is written.

The test author can fake only what the stubs name, and declines a criterion whose
behaviour reaches anything else. Follow every path the requirement's behaviour takes from
the seam, and name each collaborator on it, including those the code reaches by a name
rather than a symbol: child workflows and activities it runs by their registered name,
workflows it starts or signals through a client, and messages, signals and updates it
receives. For each, the stub of the seam states its name, task queue, arguments and
result, or the shape of the payload, in the docstring of the symbol that runs it or
receives it, and stubs the module constants that hold those names, so a test can
register fakes under them and send what the code receives. A requirement that needs a
whole other workflow of the code to run, rather than a fake of it, is observed at that
workflow's own seam, or is `unobservable` when none is narrow enough. Prefer `contract` whenever HTTP
can observe the requirement, an existing seam over an added one, and choose
`unobservable` only when no seam exists or could be added this way.

Before finishing, verify every `component` route from the test author's point of view
(assuming zero access to service source):
- Every argument and return type can be constructed or read from the stubs alone (no
  unstubbed custom types or unexplained dict shapes).
- Any file, directory, environment variable, or session/worker state resolved implicitly
  by an ID or global (rather than passed as an argument) is either avoided via an
  explicit-input seam or has its lookup/config symbols stubbed with exact paths,
  preconditions, and failure behavior stated in the docstring (or `reason` for a
  published symbol).
- Every external or side-effecting collaborator is stubbed for faking or named as running
  as it is.

{parts}{instructions}

When the input has a `declined` entry, the criteria it lists were routed before, to the
`seam` or the `page` it names or else to the contract tests, and the test author found
them unobservable there for the `reason` it gives, having seen only the stubs, the page
contract or the API. Read each reason as what the stubs or the page contract lacked:
route the criterion again with a contract that gives the author what it names, at that
seam or page or at a better one, and route it `unobservable` only when none could.
"""

PARTS_PROMPT = """
The repository holds other parts beside the service, which the `parts` entry of the input
lists by name, directory and language (the service is the part named `service`). A
requirement about the behaviour of another part, such as what a web client shows, decides
or sends, is `component` at a seam of that part when an in-process test of the part's own
code can show it: an exported function or class that a test can call with the inputs the
requirement is about and whose result or effect it can observe, such as the function a
page uses to decide what it lists, or the one that builds what the client sends, with the
browser APIs it touches faked (the test runs in jsdom). Name that part in the seam's
`part` and write its contract as its language's instructions below say; a seam of the
service names no part. Such a part is code of the repository, not an outside system. Still
prefer `contract` whenever the service's HTTP API shows the behaviour, and a seam of the
service when the service decides it. A seam the part lacks may be added as for the
service: an existing function or type exported as it is, with no behaviour change. The
`reason` of such a route says what the seam shows and what it does not, such as which items
a page lists but not how they are drawn.

"""

BROWSER_PROMPT = """
A part whose `parts` entry has `browser` is a web client that can also be opened in a real
browser: such a part also takes `browser` routes, for a requirement that only the laid-out
page decides, through its layout, painting, stacking, focus or real input events: an
element cut off by a scrolling panel or covered by another, inside the window or not, its
place at a window size, shown by hover or by tap. A browser test opens a page of the
part's built client in headless Chromium at the viewports you define, with every API call
answered from fixtures you write, and checks the page with trusted helpers (on top and not
cut off at its corners and centre, inside the window). Prefer `component` whenever a
function of the part could show the requirement in jsdom (which does no layout), and
`contract` whenever the API shows it. For a `browser` route, write or extend the part's
page contract as its instructions below say, and name in the route's `page` the part,
the page's `entry`, the viewports and the elements (`roles`) the test uses; its `reason`
says what the page shows and at which viewports, and why each viewport is there. Define
the viewports from the requirement and from the client's CSS: where the bug is tied to a
window size, read the client's breakpoints (@media rules) and name only the side the bug
is on, plus the other side as a guard only when the fix could move the bug there. An
element the page lacks, such as a role or a data-testid on an element it already renders,
goes in the page's `added`: the change adds it first, with no other change.

"""


def prompt(language: dict) -> str:
    """The prompt, with the component route where the service's language or another part's
    has component contracts, and the browser route where a part's web client has browser
    tests."""
    parts = language.get('part_components') or {}
    browser = language.get('part_browser') or {}
    instructions = '\n\n'.join([*([language['components']] if language.get('components')
                                   else []), *parts.values(), *browser.values()])
    if not instructions:
        return PROMPT.format(label=language['label'], component='', component_reason='', writes='',
                             routes='"contract" or "unobservable"', seam='')
    routes = ['"contract"', '"component"', *(['"browser"'] if browser else []), '"unobservable"']
    page = (',\n  "page": {"part": "…", "entry": "/…", "viewports": ["…"], '
            '"roles": [{"role": "…", "name": "…"} or {"testid": "…"}], "added": [the same]} '
            'for `browser` only, `added` only for elements the page does not have yet'
            if browser else '')
    return PROMPT.format(
        label=language['label'],
        writes=' and the component contract in components/'
               + (' or the page contract in parts/<part>/browser/' if browser else ''),
        component=COMPONENT_PROMPT.format(
            instructions=instructions,
            parts=(PARTS_PROMPT if parts else '') + (BROWSER_PROMPT if browser else '')),
        routes=', '.join(routes[:-1]) + ' or ' + routes[-1],
        seam=(',\n  "seam": {' + ('"part": "…", ' if parts else '')
              + '"module": "…", "symbols": ["…"], "added": ["…"], '
              + '"added_elsewhere": {"<module>": ["…"]}} for `component` only, '
              + ('`part` only for a seam of a part other than the service, ' if parts else '')
              + '`added` and `added_elsewhere` only for symbols the code does not have yet'
              + page),
        component_reason=(' For `component`, `reason` says how a test drives the seam to '
                          'observe the behaviour, and `seam` names the module of its stub, '
                          'as its contract\'s instructions name modules, and the symbols of '
                          'it the test uses, by dotted name within the module (`Class` or '
                          '`Class.method`). `added` lists those of its stub\'s symbols that '
                          'the change adds first, if any, and `added_elsewhere` those it adds '
                          'first to other stubbed modules of the same part, by module.'
                          + (' For `browser`, `reason` says what the page shows and at which '
                             'viewports, and why each viewport is there; `page` names the '
                             'part, the page\'s `entry` in its pages.json, the viewports of '
                             'its viewports.json the criterion must hold in, and the elements '
                             'the test finds, with those the page lacks yet in `added`.'
                             if browser else '')))


# What a module written in path form may end with: a stub's or a source file's suffix.
MODULE_SUFFIXES = ('.pyi', '.py', '.d.ts', '.tsx', '.ts', '.java')
# How many stubbed modules a message to the agent lists.
LISTED_MODULES = 60
# How many times a failed check of the routing is sent back to the running agent.
COMPLETION_FEEDBACK = 5
STUB_PLACEHOLDER = 'TODO: state the behaviour the brief or the existing contract asks for.'


def module_key(name: str) -> str:
    """`name`, a module in dotted or path form ('app.docker_agents', 'app/docker_agents'
    or 'app/docker_agents.py'), in the dotted form two names of one module share. Only a
    name in path form (one holding a '/') loses a file suffix or a trailing `__init__`;
    a dotted name is kept as it is, so app.languages.java stays app.languages.java."""
    key = name.strip().replace('\\', '/')
    if '/' not in key:
        return key
    while key.startswith('./'):
        key = key[2:]
    key = key.strip('/')
    for suffix in MODULE_SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix):
            key = key[:-len(suffix)]
            break
    key = key.replace('/', '.')
    if key.endswith('.__init__'):
        key = key[:-len('.__init__')]
    return key


def _stub_key(module: str) -> str:
    """A stubbed module's own name in dotted form: a dotted name as it is, a TypeScript
    part's module path (stored without its file suffix) with its '/' turned into '.'."""
    return module.strip().replace('/', '.')


def match_module(name: str, modules: Iterable[str]) -> str | None:
    """The stubbed module of `modules` that `name` names: itself when stubbed under that
    name, else the only one it names in another form (a path for a dotted name), or None
    when there is none or more than one."""
    modules = list(modules)
    if name in modules:
        return name
    key = module_key(name)
    found = [module for module in modules if _stub_key(module) == key]
    return found[0] if len(found) == 1 else None


def _stubbed_modules(components: Path) -> list[str]:
    """The modules the stubs under `components` describe, as their contracts name them."""
    modules: set[str] = set()
    if not components.is_dir():
        return []
    for file in components.rglob('*'):
        if not file.is_file() or file.is_symlink():
            continue
        path = file.relative_to(components).as_posix()
        if path.endswith('.d.ts'):
            modules.add(path.removesuffix('.d.ts'))
        elif path.endswith('.pyi') or path.endswith('.java'):
            parts = path.rsplit('.', 1)[0].split('/')
            if parts[-1] == '__init__':
                parts.pop()
            if parts:
                modules.add('.'.join(parts))
    return sorted(modules)


def validate_completion(root: Path) -> None:
    """Check the routing the agent wrote under `root` while it still runs: a RuntimeError
    is sent back to it, including one for each seam module (or module of its
    added_elsewhere) that matches no stub or more than one."""
    _check_routing(root, seam_modules=True)


def validate_final(root: Path) -> None:
    """The check of the routing once no feedback is left: everything validate_completion
    checks but the seam modules, which check_seams on the host then rejects with its own
    message when they still match no stub."""
    _check_routing(root, seam_modules=False)


def _check_routing(root: Path, seam_modules: bool) -> None:
    try:
        routes = json.loads((root / ROUTES).read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError('Agent finished without a readable routes.json') from exc
    if not isinstance(routes, dict) or not isinstance(routes.get('routes'), list):
        raise RuntimeError('routes.json does not hold a list of routes')
    # The seam modules no stub matches, by the contract they were looked up in.
    unmatched: dict[Path, list[str]] = {}
    for item in routes['routes']:
        if isinstance(item, dict) and item.get('route') == 'browser':
            page = item.get('page')
            part = page.get('part') if isinstance(page, dict) else None
            if not isinstance(part, str) or not part or '..' in part.split('/'):
                raise RuntimeError(f'A page names an invalid part: {str(part)[:80]}')
            for name in ('pages.json', 'viewports.json'):
                if not (root / PARTS / part / BROWSER / name).is_file():
                    raise RuntimeError(f'A requirement is routed to a page, but '
                                       f'{PARTS}/{part}/{BROWSER}/{name} is missing')
            continue
        if not isinstance(item, dict) or item.get('route') != 'component':
            continue
        seam = item.get('seam')
        part = seam.get('part') if isinstance(seam, dict) else None
        if part is not None and (not isinstance(part, str) or '..' in part.split('/')):
            raise RuntimeError(f'A seam names an invalid part: {str(part)[:80]}')
        components = Path(PARTS, part, COMPONENTS) if part else Path(COMPONENTS)
        if not any(file.is_file() and file.name != 'runtime.json'
                   for file in (root / components).rglob('*')):
            raise RuntimeError(f'A requirement is routed to a seam, but {components}/ holds '
                               'no stub')
        modules = _stubbed_modules(root / components)
        requirement = str(item.get('requirement_id') or item.get('id') or '?')[:40]
        named = [seam.get('module')]
        elsewhere = seam.get('added_elsewhere')
        if isinstance(elsewhere, dict):
            named += list(elsewhere)
        for module in named:
            if isinstance(module, str) and module.strip() and match_module(
                    module.strip(), modules) is None:
                unmatched.setdefault(components, []).append(
                    f'{module.strip()[:200]} (seam of {requirement})')
    if unmatched and seam_modules:
        problems = []
        for components, names in unmatched.items():
            stubbed = _stubbed_modules(root / components)
            listed = ', '.join(stubbed[:LISTED_MODULES]) + (
                ', …' if len(stubbed) > LISTED_MODULES else '')
            problems.append(f'{"; ".join(names)} name(s) a module the component contract '
                            f'in {components}/ has no stub of; it stubs the modules: '
                            f'{listed or "none"}')
        raise RuntimeError(
            'Some seams name a module that matches no stub, or more than one: '
            + '. '.join(problems) + '. Name each seam module (and each module of its '
            'added_elsewhere) as its stub\'s module, as listed, or write the missing stub '
            'at that module\'s path.')
    if seam_modules and (root / 'repository').is_dir() and (
            root / COMPONENTS / 'runtime.json').is_file():
        _check_python_stubs(root)


def _check_python_stubs(root: Path) -> None:
    """When running in a Python routing workspace, catch placeholder docstrings and
    unstubbed collaborators while the agent still has turns to fix them."""
    placeholders = [
        file.relative_to(root).as_posix()
        for file in sorted((root / COMPONENTS).rglob('*.pyi'))
        if file.is_file() and not file.is_symlink()
        and STUB_PLACEHOLDER in file.read_text(encoding='utf-8', errors='replace')
    ]
    if placeholders:
        raise RuntimeError(
            'These stubs still hold placeholder docstrings: '
            + ', '.join(placeholders[:20])
            + '. Replace each placeholder with the behaviour the symbol has to have.')
    make_stub = root.parent / 'tools' / 'make_stub.py'
    if make_stub.is_file():
        result = subprocess.run([sys.executable, str(make_stub), '--check'], cwd=root,
                                capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(detail or 'make_stub.py --check failed')


async def main() -> None:
    context = await run_sync(Path('/workspace/input.json').read_text)
    language = json.loads(await run_sync(LANGUAGE.read_text))
    await run_agent(AgentProfile(
        root=ROOT, prompt=prompt(language),
        initial_messages=[context],
        max_turns_env='ROUTING_MAX_TURNS', validate_completion=validate_completion,
        completion_feedback=COMPLETION_FEEDBACK, final_validation=validate_final,
        completion_message='Routing completed',
        exhaustion_message='Routing exceeded the tool-turn limit',
        contracts=CONTRACTS if await run_sync(CONTRACTS.is_dir) else None,
        build_dirs=language['build_dirs'],
    ))


if __name__ == '__main__':
    configure_container_logging()
    asyncio.run(main())
