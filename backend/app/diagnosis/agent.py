"""Container-only reviewer. It decides which side failed verification; it repairs nothing."""
import json
from pathlib import Path

from app.agent_workflows.runtime import AgentProfile, run_agent

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


def validate_completion(root: Path) -> None:
    try:
        verdict = json.loads((root / VERDICT).read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError('Agent finished without a readable verdict.json') from exc
    if not isinstance(verdict, dict) or verdict.get('target') not in {'implementation', 'tests'}:
        raise RuntimeError('Agent verdict does not name the implementation or the tests')
    if verdict['target'] == 'tests' and not verdict.get('tests'):
        raise RuntimeError('Agent verdict against the tests lists no broken test')


def main() -> None:
    context = Path('/workspace/input.json').read_text()
    language = json.loads(LANGUAGE.read_text())
    run_agent(AgentProfile(
        root=ROOT, prompt=(PROMPT.format(label=language['label'],
                                         report_pattern=language['report_pattern'])
                           + (CHANGE_PROMPT if json.loads(context).get('brief') else '')),
        initial_messages=[context],
        max_turns_env='DIAGNOSIS_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Diagnosis completed',
        exhaustion_message='Diagnosis exceeded the tool-turn limit',
        contracts=CONTRACTS if CONTRACTS.is_dir() else None,
        build_dirs=language['build_dirs'],
    ))


if __name__ == '__main__':
    main()
