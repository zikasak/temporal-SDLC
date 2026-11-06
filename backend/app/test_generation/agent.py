"""Container-only test author. Its filesystem contains contracts and tests only."""
import asyncio
import json
from functools import partial
from pathlib import Path

from app.asyncio_utils import run_sync
from app.agent_workflows.runtime import AgentProfile, configure_container_logging, run_agent

# Written instead of tests when a criterion of the brief cannot be observed over HTTP.
UNOBSERVABLE = 'unobservable.json'

ROOT = Path('/workspace/tests')
OPENAPI = Path('/workspace/openapi.yaml')
# Supplied instead of the embedded OpenAPI contract when the contracts are too large.
CONTRACTS = Path('/workspace/contracts')

# The language's authoring instructions and test layout, written by the host.
LANGUAGE = Path('/workspace/language.json')

PROMPT = """Write a standalone HTTP test suite in /workspace/tests
for the service described by the supplied requirements and OpenAPI contract.
The implementation is being written independently in another container. Its source,
image and logs are unavailable. Derive all expected behavior from the contract.
{instructions}
Read SERVICE_BASE_URL from
the environment and fail clearly if it is missing or the service cannot be reached.
Use bounded HTTP connection and request timeouts. Never import implementation classes,
create production sources, implement the service, or mock the behavior being tested.

Cover each endpoint's documented success response, required inputs, schema constraints,
documented errors and applicable business rules. Test multi-step behavior and persistence
through HTTP when the requirements specify it. Use independent expected values. Avoid
duplicate cases, assertion-free probes, source inspections, invented contracts and
tests that only verify their own fixtures. Each test must protect observable behavior
and fail for a credible regression. Do not disable/skip tests or swallow failures.
Set up test data through documented public APIs and clean it up when supported.
Keep tests independent and repeatable; do not assume implementation class names,
database structure, existing data or unspecified response ordering.

Use only the supplied requirements and OpenAPI contract as your authority. Document
required environment credentials and any contract ambiguities in README.md. Credentials
must come from environment variables; never put secrets in files.
Create files with write_file and change existing files with edit_file, not by
rewriting them. Locate code with list_files and search_files, read only the relevant
line ranges with read_file, and use shell to adjust and build test dependencies.
The service is not available during authoring; do not execute service tests here.
The outer worker builds the test runner image and independently compiles the suite.
Only finish when meaningful tests, a successful build and README are complete.
"""

REPOSITORY_PROMPT = """
/workspace/tests is the contract-tests directory of the project's Git repository, named by
the `repository` entry of the input. Every file you read or change is inside it, and it is
pushed to a new branch of the repository once the service passes verification. It may
already hold tests from an earlier version of the contract: keep those the supplied
contract supports and correct or remove the rest. Leave a test you keep exactly as it is:
do not rename, restructure or rewrite it. Every existing test you change or remove is
afterwards run, as it was, against the new service, and the task fails if it still passes.
"""

ONBOARDING_PROMPT = """
The service already exists: the supplied requirements and OpenAPI contract were drafted
from its code, and the suite is written anew for it, replacing any contract tests the
repository had. It must pass against the service as it is. Test only behaviour the
contract states precisely, and never rely on a detail it leaves open.
"""

CHANGE_PROMPT = """Add regression tests to the HTTP test suite in /workspace/tests, the
contract-tests directory of the project's Git repository named by the `repository` entry
of the input. The `brief` entry of the input describes a change to the running service:
the problem, the expected behaviour and the acceptance criteria. The supplied requirements
and OpenAPI contract describe the service, whose API stays exactly as it is.
The brief's `retired` entry lists the requirements of that contract the change ends:
they no longer hold once it is made, whatever the contract still says.
The implementation is changed independently in another container. Its source, image and
logs are unavailable. Derive all expected behavior from the brief and the contract.
{instructions}
Write tests that fail while the problem exists and pass once the expected behaviour is
in place: one for each acceptance criterion, asserting the expected behaviour through
HTTP exactly as the brief states it. The suite is first run against the service as it is
today, where every test you add must fail and every existing test must pass; a new test
that passes there does not reproduce the problem and stops the task.
Add each test where it belongs: in the existing test class that covers the behaviour, or
in a new class. Do not change, rename or delete any existing test: they keep protecting
the rest of the service, and the outer worker rejects a suite in which one of them
differs. Follow the structure, helpers and conventions the existing tests use.

Read SERVICE_BASE_URL from the environment, as the existing tests do, and use bounded HTTP
connection and request timeouts. Never import implementation classes, create production
sources, implement the service, or mock the behavior being tested. Use independent
expected values. Avoid assertion-free probes, source inspections, invented contracts and
tests that only verify their own fixtures. Do not disable/skip tests or swallow failures.
Set up test data through documented public APIs and clean it up when supported. Keep
tests independent and repeatable; do not assume implementation class names, database
structure, existing data or unspecified response ordering.

Use only the brief and the supplied requirements and OpenAPI contract as your authority.
Credentials must come from environment variables; never put secrets in files.
Create files with write_file and change existing files with edit_file, not by
rewriting them. Locate code with list_files and search_files, read only the relevant
line ranges with read_file, and use shell to adjust and build test dependencies.
The service is not available during authoring; do not execute service tests here.
The outer worker builds the test runner image and independently compiles the suite.
Only finish when the regression tests and a successful build are complete.
"""

DECLINE_PROMPT = """Never write a test for such a criterion that skips
itself, passes vacuously or asserts something else instead. Write /workspace/tests/{file}
with write_file, containing exactly this JSON object, and finish:
{{"unobservable": [{{"requirement_id": "FR-…", "reason": "…"}}]}}
with one entry for each such criterion: `requirement_id` is the `id` of its requirement in
the brief, and `reason` says in one or two sentences, for the user who wrote it, what a
test would need that the suite does not have. The delivery then stops and the user is
asked to rephrase those criteria; any test you wrote is discarded. Do this only when you
are sure that no test of the suite can observe the criterion.
""".format(file=UNOBSERVABLE)

UNOBSERVABLE_PROMPT = """
The suite runs in its own container against the service built from the repository's root
Dockerfile and started alone, and reaches it only over HTTP. An acceptance criterion may
need something the suite does not have to be observed, such as an outside system the
service depends on that is not running beside it, an event only such a system sends, or
state the public API cannot create. """ + DECLINE_PROMPT

ROUTES_PROMPT = """
The `routes` entry of the input lists the acceptance criteria of the brief this suite
proves, each with the requests that set it up and observe it as the routing found them.
Write one test for each of them, and none for a criterion of the brief it does not list:
those are proved at seams of the code by component tests another author writes.
"""

COMPONENT_PROMPT = """Add regression tests to the component test suite in /workspace/tests,
the component-tests directory of the project's Git repository named by the `repository`
entry of the input. The `brief` entry of the input describes a change to the service: the
problem, the expected behaviour and the acceptance criteria. The project's HTTP contract
tests cannot observe the criteria in the `routes` entry, so each of them is observed at a
seam of the service's code instead: `seam` names the dotted module and the symbols of it a
test uses, and `reason` says how a test drives it. Another author writes the HTTP tests of
the brief's other criteria: write none for them.

The component contract is in /workspace/contracts/components: a stub of each module of
the seams, at the module's path, with every symbol a test may use (its signature, types
and a docstring stating its behaviour), and the collaborators the seam calls, such as
activities, repositories and clients, as signatures only. Read it with read_contract and
search_contracts (names such as components/app/module.pyi, or
components/com/example/Flows.java for the Java type com.example.Flows, whose seam module is
that type). The service's source, image and logs are unavailable. Derive all expected
behaviour from the brief, the stubs' docstrings and the supplied requirements.
{instructions}
Write tests that fail while the problem exists and pass once the expected behaviour is in
place: one for each criterion of `routes`, asserting the expected behaviour through the
stubbed symbols exactly as the brief states it. The code under test is the seam itself:
drive it through the symbols its stub declares and never fake, patch or subclass it, or
anything else of the service but the collaborators the stubs name. Replace every one of
those with a fake of your own that has the stubbed signature and records or answers the
calls as the test needs. Never use anything of the service's code the stubs do not
declare. The suite is first run against the code as it is today, where every test you add
must fail and every existing test must pass; a new test that passes there does not
reproduce the problem and stops the task.
Add each test where it belongs: in the existing test class that covers the behaviour, or
in a new class. Do not change, rename or delete any existing test: the outer worker
rejects a suite in which one of them differs. Follow the structure, helpers and
conventions the existing tests use.

Use independent expected values and bounded waits. Avoid assertion-free probes, source
inspections, invented contracts and tests that only verify their own fakes. Do not
disable/skip tests or swallow failures. Keep tests independent and repeatable; do not
assume anything of the code the stubs do not state. Use only the brief, the stubs and the
supplied requirements as your authority. Credentials must come from environment
variables; never put secrets in files.
Create files with write_file and change existing files with edit_file, not by rewriting
them. Locate code with list_files and search_files, read only the relevant line ranges
with read_file, and use shell to adjust and check test dependencies. The outer worker
builds the suite with the service's code and collects it, and runs it later. Only finish
when the regression tests are complete and checked.
"""

COMPONENT_PARTS_PROMPT = """
The seams are in more than one part of the repository, or in a part other than the
service, such as a web client beside it, and the `parts` entry of the input lists them.
A seam of a part other than the service names it (`part`, its directory), and its module
is a path within the part. The suite has a project of its own for each part, as the
instructions above lay out: the tests of a criterion go in the project of its seam's part,
from that part's stubs in /workspace/contracts/parts/<part>/components. The names of a
part's tests in `repair` and in the runs start with the part's name and a colon.
"""

COMPONENT_UNOBSERVABLE_PROMPT = """
A criterion of `routes` may turn out not to be observable through the stubbed symbols
after all: the behaviour it is about happens outside every symbol the stubs declare, or
needs a collaborator the stubs do not name. """ + DECLINE_PROMPT

COMPONENT_REPAIR_PROMPT = """
This is a repair run. /workspace/tests already holds your previous component suite. It ran,
built with the changed code, but some tests failed, and a reviewer who compared them with
the brief and the stubs concluded that those tests, not the code, are wrong. The `repair`
entry of the input holds the reviewer's `instructions`, the failing `test_failures` and
`broken_tests`: the tests the reviewer found wrong. Those are the only tests you may
change. Every other test must stay exactly as it is, including a failing one, and the
outer worker rejects a suite in which one of them differs or is missing.
Correct each broken test with targeted edits so that it asserts exactly what the brief and
the stubs' docstrings state; do not start over. Never make a test pass by weakening it: do
not disable tests, drop assertions the brief supports, or fake the seam. Remove an
assertion, or a whole broken test, only when neither the brief nor a docstring supports
what it asserts. The corrected tests are run against the code as it was before the change
again, where each one you added must still fail. The brief and the stubs remain the
authority: where the instructions contradict them, follow them. The service's source
stays unavailable.
"""

COMPONENT_REVIEW_PROMPT = """
This is a correction run after a code review. /workspace/tests already holds your previous
component suite, which passes with the changed code. A code review of the delivery's
changes found problems the component tests must correct. The `repair.review` entry of the
input holds the `findings` sent to you (each with its summary, the file it is about, when
it is about one, and why it was sent to you) and, when present, the user's `instructions`
about them, which take precedence over the findings. Correct each finding with targeted
edits; do not start over or change tests the findings do not concern. Never weaken a test
to make it pass. The brief and the stubs remain the authority. The service's source stays
unavailable.
"""

COMPONENT_BOUND_PROMPT = """
This is a repair run. /workspace/tests already holds your previous component suite. It ran
against the code as it was before the change, where the runner stopped some of your tests
at its per-test time limit: they waited for what the change is to do with no bound of their
own. The `repair` entry of the input holds the `instructions`, those tests' `test_failures`
and `broken_tests`: the tests that hung. Those are the only tests you may change. Every
other test must stay exactly as it is, and the outer worker rejects a suite in which one of
them differs or is missing.
Find each wait in them that the code without the change may never satisfy, such as the
result of a workflow that keeps waiting, a loop polling a query or a description until a
status changes, an event or queue a fake fills, or a fake that itself waits, and bound it
to a few seconds so that the test fails with an assertion that names what did not happen.
Keep what each test asserts; do not weaken or remove assertions. The corrected tests run
against the code without the change again, where each one must fail within its bounds.
The service's source stays unavailable.
"""


BOUND_PROMPT = """
This is a repair run. /workspace/tests already holds your previous suite. It ran against
the service as the repository holds it, where the runner stopped some of your tests at its
per-test time limit: they waited for what the change is to do with no bound of their own.
The `repair` entry of the input holds the `instructions`, those tests' `test_failures` and
`broken_tests`: the tests that hung. Those are the only tests you may change. Every other
test must stay exactly as it is, and the outer worker rejects a suite in which one of them
differs or is missing. Bound each of their waits for something the service may never do,
such as polling until a state changes or a request without a timeout, to a few seconds, so
that the test fails with an assertion that names what did not happen. Keep what each test
asserts; do not weaken or remove assertions. The implementation stays unavailable.
"""


COMPONENT_REPRODUCE_PROMPT = """
This is a repair run. /workspace/tests already holds your previous component suite. It ran
against the code as it was before the change, where some of the regression tests you added
did not fail as they must: the `repair` entry of the input holds the `instructions`, the
added tests that passed there (`passing_tests`), those that did not run at all
(`missing_tests`) and, as `broken_tests`, all of them. Those are the only tests you may
change. Every other test must stay exactly as it is, and the outer worker rejects a suite in
which one of them differs or is missing.
A test that passes without the change does not observe what the brief says is wrong: it
asserts too little, asserts what the code already does, sets up a case the problem does not
arise in, or observes a symbol other than the seam's. Read the brief and the stubs'
docstrings again and correct each such test with targeted edits so that it asserts exactly
the behaviour they require, which the code without the change lacks. Make each test that
did not run be collected and run. Never make a test fail unconditionally, nor assert what
neither the brief nor a docstring supports: every test must pass once the change is made.
The corrected tests run against the code without the change again, where each one you added
must fail. The service's source stays unavailable.
"""


REPRODUCE_PROMPT = """
This is a repair run. /workspace/tests already holds your previous suite. It ran against
the service as the repository holds it, where some of the regression tests you added did
not fail as they must: the `repair` entry of the input holds the `instructions`, the added
tests that passed there (`passing_tests`), those that did not run at all (`missing_tests`)
and, as `broken_tests`, all of them. Those are the only tests you may change. Every other
test must stay exactly as it is, and the outer worker rejects a suite in which one of them
differs or is missing.
A test that passes against the service as it is does not observe what the brief says is
wrong: it asserts too little, asserts what the service already does, or sends a request the
problem does not arise for. Read the brief and the contract again and correct each such
test with targeted edits so that it asserts exactly the behaviour they require, which the
service does not have yet. Make each test that did not run be collected and run. Never make
a test fail unconditionally, nor assert what the brief and the contract do not support:
every test must pass once the problem is fixed. The corrected tests run against the service
as the repository holds it again, where each one you added must fail. The implementation
stays unavailable.
"""


REPAIR_PROMPT = """
This is a repair run. /workspace/tests already holds your previous suite. It compiled, but
some tests failed against the running service and a reviewer who compared them with the
contract concluded that those tests, not the service, are wrong. The `repair` entry of the
input holds the reviewer's `instructions`, the failing `test_failures` and `broken_tests`:
the tests the reviewer found wrong. Those are the only tests you may change. Every other
test must stay exactly as it is, including a failing one, and the outer worker rejects a
suite in which one of them differs or is missing.
Correct each broken test with targeted edits so that it asserts exactly what the contract
states; do not start over. Never make a test pass by weakening it: do not disable tests,
drop assertions the contract supports, or accept any response. Remove an assertion, or a
whole broken test, only when the contract does not support what it asserts. The
requirements and OpenAPI contract remain the authority: where the instructions contradict
them, follow the contract. The implementation stays unavailable.
"""


REVIEW_PROMPT = """
This is a correction run after a code review. /workspace/tests already holds your previous
suite, which passes against the service. A code review of the delivery's changes found
problems the tests must correct. The `repair.review` entry of the input holds the
`findings` sent to you (each with its summary, the file it is about, when it is about one,
and why it was sent to you) and, when present, the user's `instructions` about them, which
take precedence over the findings. Correct each finding with targeted edits; do not start
over or change tests the findings do not concern. Never weaken a test to make it pass. The
requirements and OpenAPI contract remain the authority. The implementation stays
unavailable.
When `repair.review` holds `restore`, it maps tests of the repository, by identity
(`module.Class#test` or `package.Class#method`), to their source as the repository holds
them, with layout and comments normalized (a method's source may follow the headers of the
classes it runs in). Put each one back in your suite exactly as
given, in the file and class it belongs to, replacing your version of it: the outer
worker rejects a suite in which one of them still differs.
"""


KEEP_PROMPT = """
The `keep` entry of the input maps tests of the repository, by identity (`module.Class#test`
or `package.Class#method`), to their source as the repository holds them, with layout and
comments normalized (a method's source may follow the headers of the classes it runs in).
Each one was run, as the repository holds
it, against the new service and passed there: the contract still supports it. Keep each
exactly as given, in the file and class it belongs to, whatever else asks to change it,
including `broken_tests`: the outer worker rejects a suite in which one of them differs or
is missing. When one of them fails in your suite, it is not wrong: something your suite
changed around it is, such as a fixture, helper, configuration or another test it shares
state with. Correct that instead, so that it passes in your suite as it does in the
repository's.
"""


def repair_prompt(inputs: dict) -> str:
    keep = KEEP_PROMPT if inputs.get('keep') else ''
    repair = inputs.get('repair')
    if not repair:
        return keep
    return (REVIEW_PROMPT if repair.get('review')
            else BOUND_PROMPT if repair.get('bound')
            else REPRODUCE_PROMPT if repair.get('reproduce') else REPAIR_PROMPT) + keep


def validate_completion(test_glob: str, root: Path, test_globs: list[str] | None = None) -> None:
    """The author finished with a test file, in every project of a suite of several parts
    (`test_globs`), or named criteria it cannot test."""
    unobservable = root / UNOBSERVABLE
    if unobservable.is_file():
        try:
            items = json.loads(unobservable.read_text()).get('unobservable')
        except (OSError, ValueError, AttributeError) as exc:
            raise RuntimeError(f'{UNOBSERVABLE} is not a JSON object') from exc
        if not isinstance(items, list) or not items:
            raise RuntimeError(f'{UNOBSERVABLE} names no criterion')
        return
    for glob in test_globs or [test_glob]:
        if not any(root.glob(glob)):
            raise RuntimeError('Agent finished without test files'
                               + (f' matching {glob}' if test_globs else ''))


def prompt(inputs: dict, language: dict) -> str:
    instructions = language['instructions']
    if inputs.get('component'):
        # A change's criteria observed at seams; only its first author may decline one.
        repair = inputs.get('repair')
        return (COMPONENT_PROMPT.format(instructions=instructions)
                + (COMPONENT_PARTS_PROMPT if inputs.get('parts') else '')
                + (COMPONENT_UNOBSERVABLE_PROMPT if not repair
                   else COMPONENT_REVIEW_PROMPT if repair.get('review')
                   else COMPONENT_BOUND_PROMPT if repair.get('bound')
                   else COMPONENT_REPRODUCE_PROMPT if repair.get('reproduce')
                   else COMPONENT_REPAIR_PROMPT))
    if inputs.get('brief'):
        # A change to an existing service is told about its repository by its own prompt.
        return (CHANGE_PROMPT.format(instructions=instructions)
                + (ROUTES_PROMPT if inputs.get('routes') else '')
                # Only the first author of a change's tests may decline one.
                + ('' if inputs.get('repair') else UNOBSERVABLE_PROMPT)
                + repair_prompt(inputs))
    return (PROMPT.format(instructions=instructions)
            + (ONBOARDING_PROMPT if inputs.get('onboarding')
               else REPOSITORY_PROMPT if inputs.get('repository') else '')
            + repair_prompt(inputs))


async def main() -> None:
    context = await run_sync(Path('/workspace/input.json').read_text)
    inputs = json.loads(context)
    language = json.loads(await run_sync(LANGUAGE.read_text))
    await run_agent(AgentProfile(
        root=ROOT, prompt=prompt(inputs, language),
        initial_messages=[context,
                          *(['OpenAPI contract:\n' + (await run_sync(OPENAPI.read_text))]
                            if await run_sync(OPENAPI.is_file) else [])],
        max_turns_env='TEST_GENERATION_MAX_TURNS',
        validate_completion=partial(validate_completion, language['test_glob'],
                                    test_globs=language.get('test_globs')),
        completion_message='Test generation completed',
        exhaustion_message='Test generation exceeded the tool-turn limit',
        contracts=CONTRACTS if await run_sync(CONTRACTS.is_dir) else None,
        build_dirs=language['build_dirs'],
    ))


if __name__ == '__main__':
    configure_container_logging()
    asyncio.run(main())
