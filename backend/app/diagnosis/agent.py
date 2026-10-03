"""Container-only reviewer. It decides which side failed verification; it repairs nothing."""
import json
from pathlib import Path

from app.agent_workflows.runtime import AgentProfile, run_agent

ROOT = Path('/workspace/review')
# Present only when the contracts were too large to embed in input.json.
CONTRACTS = Path('/workspace/contracts')
VERDICT = 'verdict.json'

PROMPT = """A generated Spring Boot service failed HTTP contract tests that were written
independently from the same requirements and OpenAPI design. Decide which side is wrong:
the implementation or the tests. The supplied requirements and API design are the only
authority; neither the implementation nor the tests are evidence of correct behavior.

/workspace/review contains, read-only for your purposes:
- service/ the implementation sources
- tests/ the test suite sources
- verification/ the failed run: reports/TEST-*.xml with assertion messages and stack
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
{"target": "implementation" or "tests",
 "summary": "one short paragraph explaining the decision",
 "instructions": "what must be corrected"}
The author who repairs the chosen side receives only `instructions` and the failing test
names and messages: the implementation author never sees the tests and the test author
never sees the implementation. Make `instructions` self-contained: for every defect name
the endpoint or requirement, quote what the contract requires, and state the observed
wrong behavior and the required correction. Only finish once verdict.json is written.
"""


def validate_completion(root: Path) -> None:
    try:
        verdict = json.loads((root / VERDICT).read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError('Agent finished without a readable verdict.json') from exc
    if not isinstance(verdict, dict) or verdict.get('target') not in {'implementation', 'tests'}:
        raise RuntimeError('Agent verdict does not name the implementation or the tests')


def main() -> None:
    run_agent(AgentProfile(
        root=ROOT, prompt=PROMPT,
        initial_messages=[Path('/workspace/input.json').read_text()],
        max_turns_env='DIAGNOSIS_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Diagnosis completed',
        exhaustion_message='Diagnosis exceeded the tool-turn limit',
        contracts=CONTRACTS if CONTRACTS.is_dir() else None,
    ))


if __name__ == '__main__':
    main()
