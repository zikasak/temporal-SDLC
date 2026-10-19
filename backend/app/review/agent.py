"""Container-only code reviewer. It reports problems in the delivery's own changes that
the passing tests do not cover; it changes nothing."""
import asyncio
import json
from pathlib import Path

from app.asyncio_utils import run_sync
from app.agent_workflows.runtime import AgentProfile, run_agent

ROOT = Path('/workspace/review')
# Present only when the contracts were too large to embed in input.json.
CONTRACTS = Path('/workspace/contracts')
FINDINGS = 'findings.json'
TARGETS = {'implementation', 'tests', 'both'}

# What the session's language calls itself, written by the host.
LANGUAGE = Path('/workspace/language.json')

PROMPT = """You review the changes a delivery made to a {label} service and to its HTTP
contract tests. The implementation and the tests were written independently from the
same requirements and OpenAPI design, and the tests already pass against the service.
The supplied requirements and API design are the authority.

/workspace/review contains, read-only for your purposes:
- service/ the implementation sources after the delivery
- tests/ the contract test sources after the delivery
- changes.diff the delivery's own changes to both, as a unified diff
The `changes` entry of the input lists the changed files of each side.

Review only these changes. Never report anything about code the delivery did not change,
even where it is wrong. Look for real problems that the passing test suite does not cover:
behaviour the contract requires that is missing or wrong but untested, unhandled errors
and edge cases, security problems such as leaked secrets or missing authorization,
concurrency and resource leaks, data loss, and tests that assert something the contract
does not state or that cannot fail. Read the diff first, then read only the relevant
line ranges of the surrounding code with read_file; locate code with search_files.

Not every observation is worth reporting. Style, naming, formatting, refactoring ideas,
missing comments, speculative performance concerns and matters of taste are not findings.
Report only problems significant enough to need a correction, each one once. An empty list
is the right answer when the changes have none.

Send each finding to the worker that has to correct it: "implementation" when the service
must change, "tests" when the tests must change, "both" when both must. The implementation
author never sees the tests and the test author never sees the implementation, so make each
summary self-contained: name the endpoint or requirement, quote what the contract requires,
state the wrong or missing behaviour and the required correction.

Work within the tool-turn budget: prioritize significant changed behaviour, keep track of
confirmed findings, and finish when the remaining-turn notice tells you to wrap up. Do not
expand the review into another investigation at that point or invent findings to fill gaps.

Do not change service/ or tests/ and do not run the tests. Finish by writing findings.json
in the project root with write_file, containing exactly this JSON object:
{{"findings": [{{"summary": "what is wrong and why it needs a correction",
                "path": "service/... or tests/... the changed file, when it is about one",
                "target": "implementation" or "tests" or "both",
                "reason": "briefly, why that worker"}}]}}
Writing findings.json ends the review immediately, so write it only when the findings are
final. No additional tool call or final text reply is needed.
"""

CHANGE_PROMPT = """
The service existed before this delivery and was changed for the task in the `brief` entry
of the input: the problem, the expected behaviour and the acceptance criteria. The brief has
the same authority as the contract, and overrides it where it states the expected behaviour
more precisely. The brief's `retired` entry lists the requirements of the contract the
change ends: they no longer hold, whatever the contract still says.
"""

INSTRUCTIONS_PROMPT = """
The user gave instructions about the findings of the previous review, in the
`instructions` entry of the input. The latest fixes were made from them: take them into
account and do not report again what the user decided should stay as it is.
"""


def validate_completion(root: Path) -> None:
    try:
        result = json.loads((root / FINDINGS).read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError('Agent finished without a readable findings.json') from exc
    if not isinstance(result, dict) or not isinstance(result.get('findings'), list):
        raise RuntimeError('findings.json does not hold a findings list')
    for item in result['findings']:
        if (not isinstance(item, dict) or item.get('target') not in TARGETS
                or not str(item.get('summary') or '').strip()):
            raise RuntimeError('Every finding needs a summary and a target of '
                               'implementation, tests or both')


async def main() -> None:
    context = await run_sync(Path('/workspace/input.json').read_text)
    inputs = json.loads(context)
    language = json.loads(await run_sync(LANGUAGE.read_text))
    await run_agent(AgentProfile(
        root=ROOT, prompt=(PROMPT.format(label=language['label'])
                           + (CHANGE_PROMPT if inputs.get('brief') else '')
                           + (INSTRUCTIONS_PROMPT if inputs.get('instructions') else '')),
        initial_messages=[context],
        max_turns_env='REVIEW_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Code review completed',
        exhaustion_message='Code review exceeded the tool-turn limit',
        completion_file=FINDINGS,
        contracts=CONTRACTS if await run_sync(CONTRACTS.is_dir) else None,
        build_dirs=language['build_dirs'],
    ))


if __name__ == '__main__':
    asyncio.run(main())
