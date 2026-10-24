"""Container-only code reviewer. It reports problems in the delivery's own changes that
the passing tests do not cover; it changes nothing."""
import asyncio
import json
from pathlib import Path

from app.asyncio_utils import run_sync
from app.agent_workflows.runtime import AgentProfile, configure_container_logging, run_agent

ROOT = Path('/workspace/review')
# Present only when the contracts were too large to embed in input.json.
CONTRACTS = Path('/workspace/contracts')
FINDINGS = 'findings.json'
TARGETS = {'implementation', 'tests', 'both'}

# What the session's language calls itself, written by the host.
LANGUAGE = Path('/workspace/language.json')

FINDINGS_FORMAT = """{"findings": [{"summary": "what is wrong and why it needs a correction",
              "path": "service/... or tests/... the changed file, when it is about one",
              "target": "implementation" or "tests" or "both",
              "reason": "briefly, why that worker",
              "repeats": the number of the earlier finding it reports again, only then}]}"""
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

Older tool results leave your context as it fills, and nothing you only thought survives
that. Whenever you suspect or confirm a problem, or finish judging a changed region, record
it with the note tool in the same turn as your next reads: the file and lines, the verdict,
and for a problem the contract statement it breaks and the evidence. Do not reread a region
your notes already judge.

Do not change service/ or tests/ and do not run the tests. Finish by writing findings.json
in the project root with write_file, containing exactly this JSON object:
{findings_format}
Writing findings.json ends the review immediately, so write it only when the findings are
final. No additional tool call or final text reply is needed.
"""

# The reviewer's progress is its judgments, not files on disk, so the checkpoint keeps them.
CHECKPOINT_PROMPT = """You keep the working notes of a code reviewer. The oldest part of its
tool history is about to be removed from its context. Write the checkpoint it will continue
from, merging any previous checkpoint with the history below. Use these sections:
Suspected problems: each problem the history or its notes point to, with the changed file and
lines, the contract or brief statement it breaks, the evidence seen and what is unverified.
Reviewed: changed files and line ranges already read and judged, with the verdict, so they
are not read again.
Remaining: changed files and ranges not yet reviewed, most significant first.
Do not reproduce code. Be specific and concise. Record only what the history shows; never
guess.
"""
# Used when the reviewer's turns run out before it wrote findings.json.
WRAP_UP_PROMPT = f"""A code reviewer ran out of its tool-turn budget before it wrote its
findings. From the task inputs, its notes, its checkpoint and its latest history below,
write the findings its evidence establishes, as exactly this JSON object and nothing else:
{FINDINGS_FORMAT}
Leave out what is unverified or speculative, and anything about code the delivery did not
change; an empty list is right when nothing is established. Make each summary
self-contained: name the endpoint or requirement, quote what the contract requires, state
the wrong or missing behaviour and the required correction. Send each finding to
"implementation" when the service must change, "tests" when the tests must change and
"both" when both must.
"""
PART_PROMPT = """
The delivery's changes are too large for one review, so separate reviewers each review one
part of them; the `review_part` entry of the input says which part is yours. Your part is the
files in `changes` and their diff in changes.diff. `review_part.other_changes` lists the
files the other parts review: read them where your files depend on them, but report only
problems in your own part's changes.
"""
CHANGE_PROMPT = """
The service existed before this delivery and was changed for the task in the `brief` entry
of the input: the problem, the expected behaviour and the acceptance criteria. The brief has
the same authority as the contract, and overrides it where it states the expected behaviour
more precisely. The brief's `retired` entry lists the requirements of the contract the
change ends: they no longer hold, whatever the contract still says.
"""

PARTS_PROMPT = """
The repository holds more than the service: the `repository_parts` entry of the input lists
its projects by directory and toolchain, such as a web frontend beside the service, and
service/ holds all of them. Review the delivery's changes to every part alike. The
requirements apply to the whole repository: a requirement that a part other than the service
has to meet, such as a page, form or user action of a frontend, or a client's use of an
endpoint the delivery added or changed, is a finding when the delivery left it unmet, even
if it changed no file of that part; give such a finding no path when no changed file is
about it. Send these findings to "implementation". The contract tests check only the
service's HTTP API, so nothing but this review checks the other parts.
"""

ONBOARDING_PROMPT = """
This delivery onboards an existing service: the contract was drafted from its code, and
the delivery writes the contract tests and the root Dockerfile for it. The existing code is
not part of the changes unless the user confirmed a bug in it: do not report how the
existing code could be better, only problems in what the delivery changed.
"""

INSTRUCTIONS_PROMPT = """
The user gave instructions about the findings of the previous review, in the
`instructions` entry of the input. The fixes were made from them: take them into account
and do not report again what the user decided should stay as it is.
"""

PREVIOUS_PROMPT = """
This is a later review round. The `previous_findings` entry of the input lists, numbered,
what earlier rounds found and what has since been corrected. Check each one in the changed
code first. A corrected one is settled: do not report it again, nor the same concern in
other words, nor the opposite of a correction you asked for. Report an earlier finding
again only when the code still clearly has the problem, and then set its `repeats` to that
finding's number. Otherwise hold new findings to a high bar: report only what is clearly
wrong, not further hardening of code an earlier round already judged.
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
        repeats = item.get('repeats')
        if repeats is not None and (not isinstance(repeats, int) or isinstance(repeats, bool)):
            raise RuntimeError('A finding\'s repeats must be the number of an earlier finding')


async def main() -> None:
    context = await run_sync(Path('/workspace/input.json').read_text)
    inputs = json.loads(context)
    language = json.loads(await run_sync(LANGUAGE.read_text))
    await run_agent(AgentProfile(
        root=ROOT, prompt=(PROMPT.format(label=language['label'], findings_format=FINDINGS_FORMAT)
                           + (PART_PROMPT if inputs.get('review_part') else '')
                           + (CHANGE_PROMPT if inputs.get('brief') else '')
                           + (PARTS_PROMPT if inputs.get('repository_parts') else '')
                           + (ONBOARDING_PROMPT if inputs.get('onboarding') else '')
                           + (INSTRUCTIONS_PROMPT if inputs.get('instructions') else '')
                           + (PREVIOUS_PROMPT if inputs.get('previous_findings') else '')),
        initial_messages=[context],
        max_turns_env='REVIEW_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Code review completed',
        exhaustion_message='Code review exceeded the tool-turn limit',
        completion_file=FINDINGS, checkpoint_prompt=CHECKPOINT_PROMPT, working_notes=True,
        wrap_up_prompt=WRAP_UP_PROMPT,
        contracts=CONTRACTS if await run_sync(CONTRACTS.is_dir) else None,
        build_dirs=language['build_dirs'],
    ))


if __name__ == '__main__':
    configure_container_logging()
    asyncio.run(main())
