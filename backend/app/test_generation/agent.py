"""Container-only test author. Its filesystem contains contracts and tests only."""
import json
from functools import partial
from pathlib import Path

from app.agent_workflows.runtime import AgentProfile, run_agent

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
contract supports and correct or remove the rest.
"""

REPAIR_PROMPT = """
This is a repair run. /workspace/tests already holds your previous suite. It compiled, but
some tests failed against the running service and a reviewer who compared them with the
contract concluded that those tests, not the service, are wrong. The `repair` entry of the
input holds the reviewer's `instructions` and the failing `test_failures`.
Correct each reported test with targeted edits so that it asserts exactly what the contract
states; do not start over or rewrite tests that are not reported. Never make a test pass by
weakening it: do not delete or disable tests, drop assertions the contract supports, or
accept any response. Remove an assertion only when the contract does not support it. The
requirements and OpenAPI contract remain the authority: where the instructions contradict
them, follow the contract. The implementation stays unavailable.
"""


def validate_completion(test_glob: str, root: Path) -> None:
    if not any(root.glob(test_glob)):
        raise RuntimeError('Agent finished without test files')


def main() -> None:
    context = Path('/workspace/input.json').read_text()
    inputs = json.loads(context)
    language = json.loads(LANGUAGE.read_text())
    run_agent(AgentProfile(
        root=ROOT, prompt=(PROMPT.format(instructions=language['instructions'])
                           + (REPOSITORY_PROMPT if inputs.get('repository') else '')
                           + (REPAIR_PROMPT if inputs.get('repair') else '')),
        initial_messages=[context,
                          *(['OpenAPI contract:\n' + OPENAPI.read_text()]
                            if OPENAPI.is_file() else [])],
        max_turns_env='TEST_GENERATION_MAX_TURNS',
        validate_completion=partial(validate_completion, language['test_glob']),
        completion_message='Test generation completed',
        exhaustion_message='Test generation exceeded the tool-turn limit',
        contracts=CONTRACTS if CONTRACTS.is_dir() else None,
        build_dirs=language['build_dirs'],
    ))


if __name__ == '__main__':
    main()
