"""Container-only test author. Its filesystem contains contracts and tests only."""
import asyncio
import json
from functools import partial
from pathlib import Path

from app.asyncio_utils import run_sync
from app.agent_workflows.runtime import AgentProfile, configure_container_logging, run_agent

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


def repair_prompt(inputs: dict) -> str:
    repair = inputs.get('repair')
    if not repair:
        return ''
    return REVIEW_PROMPT if repair.get('review') else REPAIR_PROMPT


def validate_completion(test_glob: str, root: Path) -> None:
    if not any(root.glob(test_glob)):
        raise RuntimeError('Agent finished without test files')


async def main() -> None:
    context = await run_sync(Path('/workspace/input.json').read_text)
    inputs = json.loads(context)
    language = json.loads(await run_sync(LANGUAGE.read_text))
    await run_agent(AgentProfile(
        # A change to an existing service is told about its repository by its own prompt.
        root=ROOT, prompt=((CHANGE_PROMPT.format(instructions=language['instructions'])
                            if inputs.get('brief')
                            else PROMPT.format(instructions=language['instructions'])
                            + (REPOSITORY_PROMPT if inputs.get('repository') else ''))
                           + repair_prompt(inputs)),
        initial_messages=[context,
                          *(['OpenAPI contract:\n' + (await run_sync(OPENAPI.read_text))]
                            if await run_sync(OPENAPI.is_file) else [])],
        max_turns_env='TEST_GENERATION_MAX_TURNS',
        validate_completion=partial(validate_completion, language['test_glob']),
        completion_message='Test generation completed',
        exhaustion_message='Test generation exceeded the tool-turn limit',
        contracts=CONTRACTS if await run_sync(CONTRACTS.is_dir) else None,
        build_dirs=language['build_dirs'],
    ))


if __name__ == '__main__':
    configure_container_logging()
    asyncio.run(main())
