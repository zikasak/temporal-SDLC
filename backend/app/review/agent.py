"""Container-only code reviewer. It reports problems in the delivery's own changes that
the passing tests do not cover; it changes nothing."""
import asyncio
import json
from pathlib import Path

from app.asyncio_utils import run_sync
from app.agent_workflows.runtime import AgentProfile, configure_container_logging, run_agent
from app.llm.questions import USER_ANSWERS_KEY, USER_ANSWERS_PROMPT

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

{work}"""

# How every review works and finishes, whatever it reviews.
WORK_PROMPT = """Work within the tool-turn budget: prioritize significant changed behaviour, keep track of
confirmed findings, and finish when the remaining-turn notice tells you to wrap up. Do not
expand the review into another investigation at that point or invent findings to fill gaps.

Older tool results leave your context as it fills, and nothing you only thought survives
that. Whenever you suspect or confirm a problem, or finish judging a changed region, record
it with the note tool in the same turn as your next reads: the file and lines, the verdict,
and for a problem the contract statement it breaks and the evidence. Do not reread a region
your notes already judge.

Do not change {readonly} and do not run the tests. Finish by writing findings.json
in the project root with write_file, containing exactly this JSON object:
{findings_format}
Writing findings.json ends the review immediately, so write it only when the findings are
final. No additional tool call or final text reply is needed.
"""

SEAM_PROMPT = """You review a seam that a change added to an existing {label} service before the
change itself is made. The `brief` entry of the input describes the change: the problem, the
expected behaviour and the acceptance criteria. Some of those criteria cannot be observed over
HTTP, so the `component_routes` entry of the input routes each of them to a seam of the code
(`seam`: a dotted module and its symbols), at which independently written component tests
will drive the code in-process, faking the collaborators it calls. The code lacked some of
the seam's symbols: the `seam` entry of the input lists them by module, and an author added
exactly those, each doing the least its signature allows, with no behaviour of its own yet.
The component tests are written from the stubs alone and have to fail against this code; the
fix is written later, from the code as the author left it, and has to make them pass.

/workspace/review contains, read-only for your purposes:
- service/ the code with the seam
- contracts/components/ the stubs: the signature, types and behaviour docstring of every
  symbol of the seam and of the collaborators the tests fake (a part P other than the
  service has its stubs in contracts/parts/P/components/)
- changes.diff the author's changes to the code, as a unified diff
The `changes` entry of the input lists the changed files.

Review only these changes. Never report anything about code the author did not change, nor
about the stubs, which no author of this delivery may change. Report:
- a listed symbol that is missing, or added with another signature, type, kind or module
  than its stub declares;
- anything changed beyond the listed symbols: code renamed, moved, removed or rewritten,
  dependencies or configuration changed, behaviour the code has today altered;
- an added symbol that already does part of what its docstring says the fix will do, or that
  is called, registered or wired in where it changes what the code does today;
- an added symbol the component tests could not drive in-process as its stub states, such
  as one not reachable from its module, or a collaborator the stub names that the code does
  not declare as the stub has it;
- build, startup, concurrency or resource problems the additions introduce.
Do not report the behaviour the docstrings describe as missing: the fix adds it later.
Style, naming the stubs fix, formatting, refactoring ideas and matters of taste are not
findings. An empty list is the right answer when the seam is right.

Every finding goes to the author of the seam: set its target to "implementation". Make each
summary self-contained: name the module and symbol, quote the stub where it matters, state
what is wrong and the required correction. Read the diff first, then read only the relevant
line ranges of the surrounding code and of the stubs with read_file; locate code with
search_files.

{work}"""

REGRESSION_PROMPT = """You review the regression tests that a change added to the tests of an
existing {label} service, before the change itself is made. The `brief` entry of the input
says what is wrong today: the problem, the expected behaviour and the acceptance criteria;
it has the same authority as the contract, and overrides it where it states the expected
behaviour more precisely. Test authors who never saw the code added tests that prove each
criterion, and those tests were run against the code as the repository holds it (or, when
the change added a seam first, against the code with only that seam added): every added
test failed there, as it must, and every existing test passed. The `baseline` entry of the
input holds how the added tests failed. The fix is written next, by an author who never
sees the tests, and every added test has to pass once it is made, whichever correct way the
fix takes.

/workspace/review contains, read-only for your purposes:
- service/ the code the tests ran against. It is not part of the changes: read it only to
  judge the tests, and never report anything about it
- tests/ the HTTP contract tests after the authors' changes, when the change has any
- changes.diff the authors' changes to the tests, as a unified diff
The `changes` entry of the input lists the changed files of each suite.

Review only these changes. Report:
- an added test that fails for another reason than the problem the brief names: a wrong
  request, setup, fixture or expectation, an error in its own code, or a failure in
  `baseline` that does not show the problem;
- an added test that asserts what neither the brief nor the contract states, or that
  depends on how the fix is made rather than on what it must achieve, so that a correct fix
  could still fail it;
- an added test that an incomplete fix would already pass, because it asserts too little of
  what its criterion requires;
- a criterion of the brief that no added test proves, or proves only in part;
- tests that are flaky, depend on order, timing or shared state, or leave state behind that
  other tests see;
- existing tests changed or removed, or fixtures, helpers or configuration other tests use
  changed, where the brief does not call for it.
Do not report what the code does wrong: that is the problem the tests prove. Style,
naming, formatting, refactoring ideas and matters of taste are not findings. An empty list
is the right answer when the tests are right.

Every finding goes to the test authors: set its target to "tests". An author sees only the
brief, the contract and its own tests, never the code, so make each summary self-contained:
name the test and the criterion, quote what the brief or the contract requires, and state
what is wrong and the required correction, without quoting the code. Read the diff first,
then read only the relevant line ranges of the tests and the code with read_file; locate
code with search_files.

{work}"""

REGRESSION_COMPONENT_PROMPT = """
Some acceptance criteria cannot be observed over HTTP, so the `component_routes` entry of
the input routes each of them to a seam of the code (`seam`: a dotted module and its
symbols), where component tests prove it in-process. Their author wrote them from the
stubs of the component contract alone, faking the collaborators the stubs name.
/workspace/review also contains, read-only for your purposes:
- component-tests/ the component test sources after the authors' changes
- contracts/components/ the stubs: the signature, types and behaviour docstring of every
  symbol the component tests use, and of the collaborators they fake
A seam may be in another part of the repository than the service (its `seam` names the
`part`): that part's tests are in component-tests/parts/P/ and its stubs in
contracts/parts/P/components/. The stubs are not part of the changes. When
`contract_tests` is false the change has no contract tests of its own, and tests/ is
absent.
Review the component tests as the contract tests, and also report a component test that
fakes, patches or subclasses the seam itself rather than its collaborators, or uses
anything of the code the stubs do not declare.
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
SETTLED_PROMPT = """
An earlier round of this review already judged some changed files and found nothing in
them, and no correction has changed them since: the `settled_changes` entry of the input
lists them. They are not in `changes` or changes.diff. Read them where your files depend on
them, but do not review them or report anything about them.
"""

CRITERIA_TESTS_PROMPT = """
The `criteria_tests` entry of the input maps each acceptance criterion to the places
(file:line) where an added or changed test names it in a `requirement:` line; the host has
checked that every criterion has one. Start from those places to judge whether each test
proves its criterion, instead of searching for which test proves what.
"""

STATIC_PROMPT = """
Trusted static tools (a secrets scanner, and for some languages a linter limited to errors,
likely bugs and security rules, such as ESLint over TypeScript and JavaScript with React's
rules and a check for sibling elements sharing a key) ran over the changes; the `static_findings` entry of the
input lists what they reported on the lines the delivery added, each with the tool, rule,
file, line and message. Judge each one: report those that are real problems of the
delivery, as your own findings, and leave out those that are not, such as test data, a
fixture or a placeholder value. Do not search for leaked secrets, undefined names, unused
imports or the other problems these rules cover yourself.
"""

CHANGE_PROMPT = """
The service existed before this delivery and was changed for the task in the `brief` entry
of the input: the problem, the expected behaviour and the acceptance criteria. The brief has
the same authority as the contract, and overrides it where it states the expected behaviour
more precisely. The brief's `retired` entry lists the requirements of the contract the
change ends: they no longer hold, whatever the contract still says.
"""

BUGFIX_PROMPT = """
The change fixes a bug, and its added tests are regression tests: each one was seen to fail
against the code as it was before the change (or, when the change added a seam first,
against the code with only that seam), and every test a correction adds or changes is held
against that code again, where it has to fail too. A test of behaviour the code already had
before the change cannot fail there, so a correction that adds one is rejected. Never ask
for a test of such behaviour, even where a criterion or a route names it: for example a
step of a criterion that code changes.diff leaves as it was already performs, when only
another part of the code, which the delivery changed, got the criterion wrong. Nor report
that such a test is missing or was removed. Ask for tests only of behaviour the delivery's
changes add or correct.
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
COMPONENT_PROMPT = """
Some acceptance criteria cannot be observed over HTTP, so the `component_routes` entry of
the input routes each of them to a seam of the code (`seam`: a dotted module and its
symbols), where component tests prove it in-process. Their author wrote them from the
stubs of the component contract alone, faking the collaborators the stubs name, and they
pass with the changed code; the fix was checked to keep every stubbed signature.
/workspace/review also contains, read-only for your purposes:
- component-tests/ the component test sources after the delivery
- contracts/components/ the stubs: the signature, types and behaviour docstring of every
  symbol the component tests use, and of the collaborators they fake
A seam may be in another part of the repository than the service, such as a web client
beside it (its `seam` names the `part`): that part's tests are in component-tests/parts/P/
and its stubs in contracts/parts/P/components/.
Their changes are in changes.diff and `changes` too; the path of a finding about one of
them starts with component-tests/, contracts/components/ or contracts/parts/. When
`contract_tests` is false
the change has no contract tests of its own, and tests/ is absent.
Review the component tests as you review contract tests, and also report a component test
that fakes, patches or subclasses the seam itself rather than its collaborators, or uses
anything of the service's code the stubs do not declare; send those to "tests".
Check every docstring of the stubs the delivery added or changed: it has to state
behaviour that the brief or the existing contract supports, as the tests rely on it, and
never how the code does it, such as its private names, internal steps or data structures.
Report each docstring that breaks this rule, with the stub's path and target "both". No
author of this delivery may change the stubs, so such a finding is left for the user.
"""

BROWSER_PROMPT = """
Some criteria are routed `browser` instead (`page`: a part, the page's `entry`, its
viewports and the elements its test finds): browser tests prove them on the part's built
web client in headless Chromium, with the API answered from fixtures. Their author wrote
them from the page contract alone. Those of part P are in component-tests/browser/P/, and
the page contract in contracts/parts/P/browser/: pages.json (the pages, the elements they
declare and the operations they call), viewports.json (the window sizes and inputs) and
fixtures/ (responses checked against the API's OpenAPI document). The tests' trusted
helpers, which the runner always supplies as they are, are in
component-tests/browser/P/tests/support/index.ts: rely on what they check (fixture,
expectFullyVisible: inside the window and on top at the corners and centre,
expectInViewport, reveal: hover by mouse and tap by touch, settle) rather than reading
geometry code; expectFullyVisible, like document.elementFromPoint, does not see an
overlay with `pointer-events: none`. Also report a browser test that asserts layout, size or position without
these helpers, that is not tagged with every viewport its criterion's `page` names, that
finds elements other than as pages.json declares them, or that would pass whether or not
the page shows the criterion, such as one that never reveals what it checks.
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


def _browser(inputs: dict) -> bool:
    """Whether a criterion of the change is proved by browser tests."""
    routes = inputs.get('component_routes')
    return isinstance(routes, dict) and any(
        isinstance(route, dict) and route.get('route') == 'browser' for route in routes.values())


def prompt(inputs: dict, label: str) -> str:
    """What the reviewer is asked: by default to review the verified delivery, or, for a
    stage review (`review_scope`), the seam a change added first or its regression tests
    before the fix."""
    scope = inputs.get('review_scope')
    part = PART_PROMPT if inputs.get('review_part') else ''
    previous = PREVIOUS_PROMPT if inputs.get('previous_findings') else ''
    # Every scope is judged against the user's answers as well as the requirements.
    previous = (USER_ANSWERS_PROMPT if inputs.get(USER_ANSWERS_KEY) else '') + previous
    settled = SETTLED_PROMPT if inputs.get('settled_changes') else ''
    if scope == 'seam':
        work = WORK_PROMPT.format(readonly='service/ or contracts/',
                                  findings_format=FINDINGS_FORMAT)
        return SEAM_PROMPT.format(label=label, work=work) + part + settled + previous
    if scope == 'regression':
        work = WORK_PROMPT.format(readonly='service/, tests/, component-tests/ or contracts/',
                                  findings_format=FINDINGS_FORMAT)
        return (REGRESSION_PROMPT.format(label=label, work=work) + part + settled
                + (REGRESSION_COMPONENT_PROMPT if inputs.get('component_routes') else '')
                + (BROWSER_PROMPT if _browser(inputs) else '')
                + (CRITERIA_TESTS_PROMPT if inputs.get('criteria_tests') else '')
                + previous)
    work = WORK_PROMPT.format(readonly='service/ or tests/', findings_format=FINDINGS_FORMAT)
    return (PROMPT.format(label=label, work=work)
            + part + settled
            + (STATIC_PROMPT if inputs.get('static_findings') else '')
            + (CHANGE_PROMPT if inputs.get('brief') else '')
            + (BUGFIX_PROMPT if inputs.get('regression') else '')
            + (PARTS_PROMPT if inputs.get('repository_parts') else '')
            + (ONBOARDING_PROMPT if inputs.get('onboarding') else '')
            + (COMPONENT_PROMPT if inputs.get('component_routes') else '')
            + (BROWSER_PROMPT if _browser(inputs) else '')
            + (INSTRUCTIONS_PROMPT if inputs.get('instructions') else '')
            + previous)


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
        root=ROOT, prompt=prompt(inputs, language['label']),
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
