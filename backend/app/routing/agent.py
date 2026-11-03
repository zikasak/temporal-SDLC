"""Container-only router. It decides which suite can prove each criterion; it writes no
test and changes no code."""
import asyncio
import json
from pathlib import Path

from app.asyncio_utils import run_sync
from app.agent_workflows.runtime import AgentProfile, configure_container_logging, run_agent

ROOT = Path('/workspace/routing')
# Present only when the contracts were too large to embed in input.json.
CONTRACTS = Path('/workspace/contracts')
ROUTES = 'routes.json'
# The component contract, beside routes.json.
COMPONENTS = 'components'

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
rephrase it. Only finish once routes.json is written.
"""

COMPONENT_PROMPT = """
A requirement the contract suite cannot observe may still be observable one level lower,
at a seam: an interface the code already has, as the repository holds it today, at which
an in-process test can bring the code into the state the requirement is about and observe
the behaviour, with everything the seam calls outside itself replaced by fakes. Such a test
is written by an author who never sees the code, only stubs of the seam you write. Good
seams already have contract-shaped interfaces: a workflow's signals, updates and queries,
which an in-process workflow test environment can drive with faked activities; a service
class whose collaborators are passed in. A requirement is `component` when it is not
`contract` and such a seam exists; name it and write its stubs. Never invent a seam: every
stubbed symbol must already exist with the stubbed signature, or the check fails. Prefer
`contract` whenever HTTP can observe the requirement, and choose `unobservable` only when
no seam exists either.

{instructions}
"""


def prompt(language: dict) -> str:
    """The prompt, with the component route where the language has component contracts."""
    if not language.get('components'):
        return PROMPT.format(label=language['label'], component='', component_reason='', writes='',
                             routes='"contract" or "unobservable"', seam='')
    return PROMPT.format(
        label=language['label'], writes=' and the component contract in components/',
        component=COMPONENT_PROMPT.format(instructions=language['components']),
        routes='"contract", "component" or "unobservable"',
        seam=',\n  "seam": {"module": "…", "symbols": ["…"]} for `component` only',
        component_reason=(' For `component`, `reason` says how a test drives the seam to '
                          'observe the behaviour, and `seam` names the dotted module of its '
                          'stub and the symbols of it the test uses, by dotted name within '
                          'the module (`Class` or `Class.method`).'))


def validate_completion(root: Path) -> None:
    try:
        routes = json.loads((root / ROUTES).read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError('Agent finished without a readable routes.json') from exc
    if not isinstance(routes, dict) or not isinstance(routes.get('routes'), list):
        raise RuntimeError('routes.json does not hold a list of routes')
    if (any(isinstance(item, dict) and item.get('route') == 'component'
            for item in routes['routes'])
            and not any(item.is_file() and item.name != 'runtime.json'
                        for item in (root / COMPONENTS).rglob('*'))):
        raise RuntimeError('A requirement is routed to a seam, but components/ holds no stub')


async def main() -> None:
    context = await run_sync(Path('/workspace/input.json').read_text)
    language = json.loads(await run_sync(LANGUAGE.read_text))
    await run_agent(AgentProfile(
        root=ROOT, prompt=prompt(language),
        initial_messages=[context],
        max_turns_env='ROUTING_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Routing completed',
        exhaustion_message='Routing exceeded the tool-turn limit',
        contracts=CONTRACTS if await run_sync(CONTRACTS.is_dir) else None,
        build_dirs=language['build_dirs'],
    ))


if __name__ == '__main__':
    configure_container_logging()
    asyncio.run(main())
