"""Container-only reviewer. It decides which side failed verification; it repairs nothing."""
import asyncio
import json
from functools import partial
from pathlib import Path

from app.asyncio_utils import run_sync
from app.agent_workflows.runtime import AgentProfile, configure_container_logging, run_agent

ROOT = Path('/workspace/review')
# Present only when the contracts were too large to embed in input.json.
CONTRACTS = Path('/workspace/contracts')
VERDICT = 'verdict.json'

# What the session's language calls itself and its test reports, written by the host.
LANGUAGE = Path('/workspace/language.json')

PROMPT = """A generated {label} service failed HTTP contract tests that were written
independently from the same requirements and OpenAPI design. Decide which side is wrong:
the implementation or the tests. The supplied requirements and API design are the only
authority; neither the implementation nor the tests are evidence of correct behavior.

/workspace/review contains, read-only for your purposes:
- service/ the implementation sources
- tests/ the test suite sources
- verification/ the failed run: reports/{report_pattern} with assertion messages and stack
  traces, service_log.log and runner_log.log
The `verification` entry of the input lists the failing tests.

For each failing test, read the test to see exactly what it sends and asserts, read the
contract clause that governs that behavior, then read the implementation code that handles
the request. Locate code with list_files and search_files and read only the relevant line
ranges with read_file. Group failures that share one cause.
The tests are wrong when they assert behavior the contract does not state or contradicts
(invented status codes, fields, formats, ordering or defaults), send requests the contract
does not allow, depend on other tests or pre-existing data, or are broken themselves.
The implementation is wrong when it deviates from the contract, lacks required behavior,
or fails internally (5xx responses, exceptions in the service log, startup problems).
Where the contract is silent or ambiguous, the tests are wrong for asserting it.
When both sides have defects, choose the side that causes the most failures; the other
side is reviewed again after the next verification.

Do not change service/ or tests/ and do not run the tests. Finish by writing verdict.json
in the project root with write_file, containing exactly this JSON object:
{{"target": "implementation" or "tests",
 "tests": [{{"class_name": "...", "name": "..."}}],
 "summary": "one short paragraph explaining the decision",
 "instructions": "what must be corrected"}}
When the target is the tests, `tests` lists every failing test that is itself wrong, with
its `class_name` and `name` copied exactly from the `verification` entry. The repair may
change those tests and no others, so a wrong test you leave out stays wrong, and a test
that did not fail cannot be listed. When the target is the implementation, `tests` is [].
The author who repairs the chosen side receives only `instructions` and the failing test
names and messages: the implementation author never sees the tests and the test author
never sees the implementation. Make `instructions` self-contained: for every defect name
the endpoint or requirement, quote what the contract requires, and state the observed
wrong behavior and the required correction. Only finish once verdict.json is written.
"""

CHANGE_PROMPT = """
The service existed before this run and was changed for the task in the `brief` entry of
the input: the problem, the expected behaviour and the acceptance criteria. The test suite
is the repository's existing one plus regression tests for that brief. The brief has the
same authority as the contract, and overrides it where it states the expected behaviour
more precisely. The brief's `retired` entry lists the requirements of the contract the change ends:
they no longer hold once it is made, whatever the contract still says.
"""

ONBOARDING_PROMPT = """
The service is the repository's existing code, being onboarded: the contract was drafted
from that code, and the code is presumed right. Choose the tests unless the service did
not build or start with its Dockerfile, or the failure plainly reveals a bug in the
existing code. The user is asked to confirm such a bug before the code is changed, so
explain in `summary` what the code does and why that contradicts the contract.
"""

COMPONENT_PROMPT = """
Besides the HTTP contract tests, the change has component tests: in-process tests of the
acceptance criteria that HTTP cannot observe, each driven through a seam of the service's
code. Another author wrote them from the stubs of the component contract alone, faking the
collaborators the stubs name, and they ran built with the changed code. /workspace/review
also contains, read-only for your purposes:
- component-tests/ the component test sources
- component-contract/ the stubs: the signatures, types and behaviour docstrings of every
  symbol the component tests may use
- component-verification/ their run, when it failed: reports/{report_pattern} and
  runner_log.log
A seam may be in another part of the repository than the service, such as a web client
beside it, whose code is in service/ under the part's directory: then the tests of part P
are in component-tests/parts/P/, its stubs in component-contract/parts/P/, and the names of
its tests, in the input and the reports, start with "P:".
A criterion may instead be routed to a page of a part's web client, proved by browser
tests in headless Chromium: then the tests of part P are in component-tests/browser/P/,
its page contract (pages.json, viewports.json and the API fixtures) in
component-contract/parts/P/browser/, the trusted helpers the tests use in
component-tests/browser/P/tests/support/, and the names of its tests start with
"P~browser:" and name the viewport they ran in (`P~browser:tests/a.spec.ts#narrow > …`).
They are the component suite too. The page contract is their authority, as stubs are.
The `component_verification` entry of the input lists their failing tests, and the
`verification` entry those of the contract tests, when the change has contract tests; the
`status` of each says whether that run failed. The brief and the stubs' docstrings are the
component tests' authority. They are wrong when they assert behaviour neither states, use
a symbol the stubs do not declare, fake or patch the seam itself rather than its
collaborators, or are broken themselves. The implementation is wrong when the seam does
not behave as the brief and the docstrings state. The fix has to keep every stubbed
signature exactly, so the correction is never to change one.
When the target is the tests, add "suite": "contract" or "component" to verdict.json: the
suite whose tests are wrong. Only that suite's author repairs them, so `tests` lists
failing tests of that suite alone; when both suites have wrong tests, choose the one with
the most and the other is reviewed again after the next verification.
"""

REPOSITORY_SUITE_PROMPT = """
The `contract_suite` entry says the contract tests are the repository's own: the change
added none, every one of them passed against the service before the change, and no author
of this delivery may change them. A contract test that fails now was broken by the change,
so the target for it is the implementation.
"""


def validate_completion(component: bool, root: Path) -> None:
    try:
        verdict = json.loads((root / VERDICT).read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError('Agent finished without a readable verdict.json') from exc
    if not isinstance(verdict, dict) or verdict.get('target') not in {'implementation', 'tests'}:
        raise RuntimeError('Agent verdict does not name the implementation or the tests')
    if verdict['target'] == 'tests' and not verdict.get('tests'):
        raise RuntimeError('Agent verdict against the tests lists no broken test')
    if (component and verdict['target'] == 'tests'
            and verdict.get('suite') not in ('contract', 'component')):
        raise RuntimeError('Agent verdict against the tests does not name their suite as '
                           '"contract" or "component"')


def prompt(inputs: dict, language: dict) -> str:
    text = (PROMPT.format(label=language['label'], report_pattern=language['report_pattern'])
            + (CHANGE_PROMPT if inputs.get('brief') else '')
            + (ONBOARDING_PROMPT if inputs.get('onboarding') else ''))
    if 'component_verification' in inputs:
        text += COMPONENT_PROMPT.format(report_pattern=language['report_pattern'])
        if inputs.get('contract_suite') == 'repository':
            text += REPOSITORY_SUITE_PROMPT
    return text


async def main() -> None:
    context = await run_sync(Path('/workspace/input.json').read_text)
    inputs = json.loads(context)
    language = json.loads(await run_sync(LANGUAGE.read_text))
    component = 'component_verification' in inputs
    await run_agent(AgentProfile(
        root=ROOT, prompt=prompt(inputs, language),
        initial_messages=[context],
        max_turns_env='DIAGNOSIS_MAX_TURNS',
        validate_completion=partial(validate_completion, component),
        completion_message='Diagnosis completed',
        exhaustion_message='Diagnosis exceeded the tool-turn limit',
        contracts=CONTRACTS if await run_sync(CONTRACTS.is_dir) else None,
        build_dirs=language['build_dirs'],
    ))


if __name__ == '__main__':
    configure_container_logging()
    asyncio.run(main())
