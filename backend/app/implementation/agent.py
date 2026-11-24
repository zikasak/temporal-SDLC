"""Container-only coding agent. No Temporal, database, or Docker access."""
import asyncio
import json
from pathlib import Path

from app.asyncio_utils import run_sync
from app.agent_workflows.runtime import AgentProfile, configure_container_logging, run_agent

ROOT = Path('/workspace/service')
# Present only when the contracts were too large to embed in input.json.
CONTRACTS = Path('/workspace/contracts')

# The language's authoring instructions, written by the host.
LANGUAGE = Path('/workspace/language.json')
# The repository's checks script: builds every part of the repository and runs its own
# tests; verification runs it too (app/verification/checks.py names the same path).
CHECKS_SCRIPT = '.sdlc/checks.sh'

PROMPT = """Implement the service in /workspace/service using the supplied
requirements and OpenAPI design. Generated interfaces and models are the API contract.
Implement real business behavior, validation, persistence and error handling.
{instructions}
Locate code with list_files and search_files and read only the relevant line ranges with
read_file. Create files with write_file; change existing files with edit_file, not by
rewriting them. Use shell for builds and anything else.
Do not generate, read, or execute tests.
Build the service without running tests. Fix build failures.
Create a production Dockerfile at the project root: multi-stage build,
skip tests, non-root runtime, expose 8080 and start the service.
Create .dockerignore excluding build output and tests. Do not put secrets in files.
You have no Docker daemon; the outer worker will build your Dockerfile.
Only finish once implementation, a successful build and Dockerfile are complete.
"""

REPOSITORY_PROMPT = """
/workspace/service is a checkout of the project's Git repository, named by the `repository`
entry of the input, {generated} Every file you read or change is inside that checkout, and
it is pushed to a new branch of the repository once the service passes verification. Read
the existing code before writing: follow its structure and conventions and extend what is
already there instead of duplicating it. Do not remove files that are unrelated to this
service.
"""
GENERATED = 'with the generated interfaces and models added to it.'
# The repository's service is laid out unlike generated code, which was left out of it.
OWN_LAYOUT = """without generated code: the service in it has a layout of its own, and the
OpenAPI design alone is the API contract. Find where the service serves its API and
implement the design there; do not add a second application, generated scaffolding or a
separate copy of the API."""

CHANGE_PROMPT = """Change the existing service in /workspace/service, a checkout of the
project's Git repository named by the `repository` entry of the input. The `brief` entry
of the input is the task: the problem, the expected behaviour and the acceptance criteria.
The supplied requirements and OpenAPI design are the contract the service was built from;
the API stays exactly as it is.
The brief's `retired` entry lists the requirements of that contract the change ends:
they no longer hold once it is made, whatever the contract still says.
{instructions}
Read the code that handles the behaviour the brief describes before changing anything.
Locate code with list_files and search_files and read only the relevant line ranges with
read_file. Make the expected behaviour true with targeted edits to the existing code,
using edit_file; create a file with write_file only when the change needs a new one. Do
not regenerate, restructure or rewrite code the brief does not concern, and do not remove
files. Use shell for builds and anything else.
Do not generate, read, or execute tests.
Build the service without running tests. Fix build failures.
Keep the Dockerfile at the project root building the service and serving it on 8080 as a
non-root user. Do not put secrets in files.
You have no Docker daemon; the outer worker will build the Dockerfile.
Every file you change is pushed to a new branch of the repository once the service passes
verification. Only finish once the change and a successful build are complete.
"""

PARTS_PROMPT = """
The repository holds more than the service: the `repository_parts` entry of the input lists
its projects by directory and toolchain, and the task covers all of them. Besides the service
they may be a web frontend, an app, a command-line tool or other code that uses its API,
often in another language. Read the requirements{brief} for what each part has to do: the
pages, forms, screens and user actions a requirement describes belong to the part that shows
them, and a part that calls an endpoint the task adds or changes must be changed to use it.
Make those changes in that part, in its own language and following its own structure and
conventions, as thoroughly as the service's; the task is not complete while a part the
requirements concern is left unchanged. Leave parts the task does not concern as they are.
The Dockerfile and the contract tests cover the service alone, so nothing else checks the
other parts: install the dependencies of each part you change and build it with its own
toolchain (for a package.json project, npm ci and its build script), without running its
tests, and fix failures before you finish.
"""
PARTS_BRIEF = ' and the brief'
COMPONENT_PROMPT = """
Some acceptance criteria cannot be observed over HTTP, so the `component_routes` entry of
the input routes each of them to a seam of the code: `seam` names the dotted module and
its symbols at which independently written component tests observe the criterion, and
`reason` says how. Those tests drive the seam in-process, faking the collaborators it
calls. The component contract in /workspace/contracts/components holds a stub of each
module of the seams, at the module's path (such as components/app/module.pyi, or
components/com/example/Flows.java for the Java type com.example.Flows): the signature,
types and behaviour docstring of every symbol the tests use, and the collaborators they
fake, as signatures only. Read it with read_contract and search_contracts. The stubs are
a contract like the API: keep every signature they declare exactly as it is, with the
same names, parameters, types, return types and kinds of symbol, in the same modules.
Make the criteria true behind those signatures, as their docstrings and the brief state.
The outer worker checks the changed code against the stubs before it runs any test, and
rejects a change that breaks one. Do not create, change or
remove contracts/components or component-tests in the checkout, and do not look for the
component tests: they stay unavailable, as the contract tests do. In a repair run the
failing tests in `repair.test_failures` may be component tests as well as contract tests.
""" + """
A seam may be in another part of the repository than the service, such as a web client
beside it: its `seam` then names the part (`part`, its directory) and the module as a path
within the part (such as src/Flows for a TypeScript part's src/Flows.tsx). That part's
stubs are in /workspace/contracts/parts/<part>/components, at the module's path (such as
src/Flows.d.ts), and the same holds for them: keep every declaration they make exactly as
it is, and do not create, change or remove contracts/parts or any component-tests
directory of a part. The component test names of such a part start with the part's name
and a colon.
""" + """
A criterion routed `browser` is observed instead on a page of a part's web client in a
real browser: its `page` names the part, the page's `entry`, the viewports (window sizes
and inputs) and the elements (`roles`) its browser tests find, which
/workspace/contracts/parts/<part>/browser describes (pages.json, viewports.json and the
API fixtures). Keep every element that contract declares findable as it says (its ARIA
role and accessible name, or its data-testid), and make the criterion true in the page as
laid out at those viewports, such as with the client's CSS. Do not create, change or remove
contracts/parts or any browser-tests directory. Those test names start with the part's
name, `~browser:` and the viewport they ran in (`frontend~browser:tests/a.spec.ts#narrow`).
"""

SEAM_PROMPT = """Prepare a change to the existing service in /workspace/service, a checkout
of the project's Git repository named by the `repository` entry of the input, without
making the change yet. The `brief` entry of the input describes it: the problem, the
expected behaviour and the acceptance criteria. Some of those criteria cannot be observed
over HTTP, so the `component_routes` entry routes each of them to a seam of the code, at
which independently written component tests will drive the code in-process, faking the
collaborators it calls. The code does not have that seam yet: you add it, and nothing else.
{instructions}
The component contract in /workspace/contracts/components holds a stub of each module of
the seams, at the module's path (such as components/app/module.pyi, or
components/com/example/Flows.java for the Java type com.example.Flows): the signature, types
and behaviour docstring of every symbol the tests use. Read it with read_contract and
search_contracts. The `seam` entry of the input lists, by module, the stubbed symbols the
code lacks. Add exactly those, in those modules, with exactly the signatures, types and
kinds of symbol the stubs declare. Every other symbol the stubs declare already exists and
stays exactly as it is.
A seam may be in another part of the repository than the service, such as a web client
beside it: the `seam` entry then names its module after the part's directory, as the
module's path in the repository (frontend/src/Flows for src/Flows within the part
frontend), and the part's stubs are in /workspace/contracts/parts/<part>/components, at the
module's path within the part (src/Flows.d.ts). An added symbol of a TypeScript part is an
`export` added to a declaration the module already has, or a new exported function that
does the least its declared type allows, with no behaviour change; build that part with
its own toolchain too.
A `seam` entry named `<part>/page:<entry>` lists elements of a page of that part's web
client, as `role=<ARIA role>[name="<accessible name>"]` or `testid=<data-testid>`, that
browser tests will find on the page at that entry, described in
/workspace/contracts/parts/<part>/browser/pages.json. An added element of a browser page
is an attribute (`role`, `aria-label` or `data-testid`) on an element the page already
renders, with no other change: no new element, style or behaviour.
The symbols you add get no behaviour of their own yet: each does the least its signature
allows and changes nothing the code does today, such as an update or signal that is
accepted and ignored, a query that returns state the code already keeps, or a method that
leaves everything as it was. A collaborator among them, such as an activity or a
constant in another module, is declared as its stub has it and nothing calls or reads it
yet. Their docstrings describe the behaviour the fix will give them later; do not
implement it, not even partly. The component tests have to fail against
the code with the seam before the fix is written, and every existing test has to pass
against it unchanged; the fix is written in a later run, from the code as you leave it.
Read the code around each symbol before adding it. Locate code with list_files and
search_files and read only the relevant line ranges with read_file, and add the symbols
with edit_file. Do not change, rename or remove anything else, and do not create, change
or remove contracts/components, contracts/parts or any component-tests or browser-tests
directory. Use shell for builds.
Do not generate, read, or execute tests.
Build the service without running tests. Fix build failures. Keep the Dockerfile at the
project root as it is. You have no Docker daemon; the outer worker builds the Dockerfile
and checks the code with the seam against the stubs.
Only finish once every listed symbol is added and the build succeeds.
"""


SEAM_REVIEW_PROMPT = """
This is a correction run after a review of the seam. /workspace/service already holds the
code with the seam as you added it. A review of your changes found problems you must
correct. The `repair.review` entry of the input holds the `findings` sent to you (each with
its summary, the file it is about, when it is about one, and why it was sent to you).
Correct each finding with targeted edits; do not start over. Everything above still holds:
add exactly the listed symbols, with the stubbed signatures and no behaviour of their own,
and change nothing else. The stubs remain the authority: where a finding contradicts them,
follow the stubs.
"""


REPAIR_PROMPT = """
This is a repair run. /workspace/service already holds your previous implementation. It
compiled, but independently written HTTP contract tests failed against the running service
and a reviewer traced the failures to the implementation. The `repair` entry of the input
holds the reviewer's `instructions`, the failing `test_failures` and the `service_log` tail.
Fix every reported defect with targeted edits to the existing code; do not start over or
rewrite code that works. The requirements and OpenAPI design remain the authority: where
the instructions contradict them, follow the contract. The test sources stay unavailable.
"""


REVIEW_PROMPT = """
This is a correction run after a code review. /workspace/service already holds your previous
implementation, which passes the contract tests. A code review of the delivery's changes
found problems the service must correct. The `repair.review` entry of the input holds the
`findings` sent to you (each with its summary, the file it is about, when it is about one,
and why it was sent to you) and, when present, the user's `instructions` about them, which
take precedence over the findings. Correct each finding with targeted edits; do not start
over or rewrite code that works, and keep everything the contract requires working. The
requirements and OpenAPI design remain the authority. The test sources stay unavailable.
A finding marked `reported_again` was sent to you before, and the review found it again
after your correction: that correction did not settle it, or a later repair undid it. Read
what it says the code still does, and settle it this time.
"""


ONBOARDING_PROMPT = """Onboard the existing service in /workspace/service, a checkout of the
project's Git repository named by the `repository` entry of the input. The supplied
requirements and OpenAPI design are the contract drafted from that code; independently
written HTTP contract tests run against the service built with your Dockerfile.
{instructions}
Do not change the existing code. Write only the Dockerfile at the project root and its
.dockerignore, replacing any the repository has, and the checks script described below. Read the build files, configuration and
entry point before writing them. The Dockerfile builds the service as a self-contained
image that the contract tests can run against alone: a multi-stage build that skips
tests, with everything the service needs to start (such as its own database inside the
image when it uses one, with any required configuration given safe defaults), serving
the API on 0.0.0.0:8080 as a non-root user. Exclude build output, tests and local
environments in .dockerignore. Do not put secrets in files.
Locate code with list_files and search_files and read only the relevant line ranges with
read_file. Use shell for builds and anything else. Do not generate, read, or execute
tests. You have no Docker daemon; the outer worker will build the Dockerfile.
Every file you write is pushed to a new branch of the repository once the tests pass.
Only finish once the Dockerfile is complete.
"""

ONBOARDING_REPAIR_PROMPT = """
This is a repair run. /workspace/service already holds your previous Dockerfile. The
`repair` entry of the input says why the service did not build, start or pass the contract
tests: its `instructions`, the failing `test_failures` and the `service_log` tail. The
existing code is right and stays exactly as it is: correct only the Dockerfile and its
.dockerignore.
"""

ONBOARDING_REVIEW_PROMPT = """
This is a correction run after a code review. The `repair.review` entry of the input holds
the `findings` sent to you. Correct those about the Dockerfile and its .dockerignore; the
existing code stays exactly as it is.
"""

CODE_CHANGE_PROMPT = """
This run may change the existing code. The `repair` entry of the input describes what to
correct: a failing contract test that the user confirmed reveals a bug in the existing
code, or the user's own `instructions` in `repair.review`, which take precedence. Make
that correction with targeted edits using edit_file, keep the Dockerfile building and
serving the service, and change nothing else of the existing code.
"""


UNIT_TESTS_PROMPT = """
An exception to what is said about tests above: the repository's own unit and integration
tests in the checkout (its test files and test directories; not the contract tests or the
component tests, which stay unavailable) are part of the code you maintain, and they are
pushed with it. Read those your change concerns. When your change alters behaviour that a
test asserts on purpose, or removes or renames something a test uses, update that test with
the change; leave every other test as it is. Never delete, skip or weaken a test only to
make it pass.
The repository's checks script, {script}, builds every part of the repository and runs
these tests; verification runs it after you finish and sends its failures back to you. When
the checkout holds it, run it with shell (`sh {script}` from the project root) before you
finish and fix what fails. Without it, build each part you changed and run its tests with
its own tool. A whole run takes minutes: give that shell call a `timeout_seconds` of 900 and
run it in one call, not split into parts or polled in the background. Run the tests your
change concerns first, on their own, to learn of a failure quickly.
""".format(script=CHECKS_SCRIPT)

CHECKS_REPAIR_PROMPT = """
This is a repair run. /workspace/service already holds your previous implementation, which
builds and passes the contract tests, but the repository's checks script failed against it:
its own builds or tests. The `repair` entry of the input holds `instructions`, an analysis of
the failure, and `checks`, the relevant part of the script's output, which starts with the
tests that failed and what each failed with. Fix the cause with targeted edits: the code
when it broke what a test rightly asserts, or a test of the repository when your change
rightly altered what it asserts. First run the failed tests on their own and see them fail;
when a failed test is not in your workspace, say so in your notes and work from its output.
Once they pass, run the whole checks script again (shell `timeout_seconds` 900, in one
call) before you finish. Do not delete, skip or weaken tests to make them pass.
"""

ONBOARDING_CHECKS_PROMPT = """
Write the repository's checks script as well: {script}, a POSIX shell script that a clean
container (Python 3.12, Java 21 with Maven, Node.js with npm, network access, not root, no
Docker) runs from the project root to check the whole repository as its CI would. It
installs each part's dependencies, builds each part and runs its own unit tests with that
part's tool, and exits with a non-zero status when any of it fails (start it with
`set -eu`). Base it on the repository's build files and any CI configuration; it must not
run the contract tests in contract-tests/ or the component tests, need a running service,
external services or secrets, or change tracked files. Keep any existing checks script and
only correct it. Run it with shell before you finish and make sure it passes on the
existing code; when an existing test fails on the existing code, leave it as it is and say
so in a comment at the top of the script.
""".format(script=CHECKS_SCRIPT)


ONBOARDING_CHECKS_REPAIR_PROMPT = """
This is a repair run. /workspace/service already holds your previous Dockerfile and checks
script, and the service passes the contract tests, but the checks script failed. The
`repair` entry of the input holds `instructions`, an analysis of the failure, and `checks`,
the relevant part of the script's output. The existing code is right and stays exactly as
it is: correct only the checks script (or the Dockerfile and its .dockerignore), and run it
again before you finish. When an existing test fails on the existing code, do not run it from
the script; say so in a comment at the top of the script.
"""


def unit_tests_prompt(inputs: dict) -> str:
    """The repository's own tests and checks script, for an author working in its checkout;
    an onboarding's author leaves the existing code, its tests included, as it is."""
    if not inputs.get('repository') or inputs.get('onboarding'):
        return ''
    return UNIT_TESTS_PROMPT


def repair_prompt(inputs: dict) -> str:
    repair = inputs.get('repair')
    if not repair:
        return ''
    if repair.get('checks') is not None:
        return ONBOARDING_CHECKS_REPAIR_PROMPT if inputs.get('onboarding') else CHECKS_REPAIR_PROMPT
    if inputs.get('seam'):
        return SEAM_REVIEW_PROMPT
    if inputs.get('onboarding'):
        if repair.get('code_change') or (repair.get('review') or {}).get('instructions'):
            return CODE_CHANGE_PROMPT
        return ONBOARDING_REVIEW_PROMPT if repair.get('review') else ONBOARDING_REPAIR_PROMPT
    return REVIEW_PROMPT if repair.get('review') else REPAIR_PROMPT


def base_prompt(inputs: dict, instructions: str) -> str:
    if inputs.get('onboarding'):
        return ONBOARDING_PROMPT.format(instructions=instructions) + ONBOARDING_CHECKS_PROMPT
    # A change to an existing service is told about its checkout by its own prompt.
    if inputs.get('brief'):
        if inputs.get('seam'):
            # Only the seam is added: nothing of the brief is fixed, no other part changes.
            return SEAM_PROMPT.format(instructions=instructions)
        return (CHANGE_PROMPT.format(instructions=instructions)
                + (COMPONENT_PROMPT if inputs.get('component_routes') else '')
                + parts_prompt(inputs))
    return (PROMPT.format(instructions=instructions) + repository_prompt(inputs)
            + parts_prompt(inputs))


def parts_prompt(inputs: dict) -> str:
    """What the author is told about the repository's projects besides the service."""
    if not inputs.get('repository_parts'):
        return ''
    return PARTS_PROMPT.format(brief=PARTS_BRIEF if inputs.get('brief') else '')


def repository_prompt(inputs: dict) -> str:
    if not inputs.get('repository'):
        return ''
    # Inputs recorded before the choice was made had the generated code added.
    return REPOSITORY_PROMPT.format(
        generated=GENERATED if inputs.get('generated_code', True) else OWN_LAYOUT)


def validate_completion(root: Path) -> None:
    if not (root / 'Dockerfile').is_file():
        raise RuntimeError('Agent finished without a Dockerfile')


async def main() -> None:
    context = await run_sync(Path('/workspace/input.json').read_text)
    inputs = json.loads(context)
    language = json.loads(await run_sync(LANGUAGE.read_text))
    await run_agent(AgentProfile(
        root=ROOT, prompt=(base_prompt(inputs, language['instructions'])
                           + unit_tests_prompt(inputs) + repair_prompt(inputs)),
        initial_messages=[context],
        max_turns_env='IMPLEMENTATION_MAX_TURNS',
        validate_completion=validate_completion,
        completion_message='Implementation completed',
        exhaustion_message='Implementation exceeded the tool-turn limit',
        contracts=CONTRACTS if await run_sync(CONTRACTS.is_dir) else None,
        build_dirs=language['build_dirs'],
    ))


if __name__ == '__main__':
    configure_container_logging()
    asyncio.run(main())
