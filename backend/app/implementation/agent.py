"""Container-only coding agent. No Temporal, database, or Docker access."""
import json
from pathlib import Path

from app.agent_workflows.runtime import AgentProfile, run_agent

ROOT = Path('/workspace/service')
# Present only when the contracts were too large to embed in input.json.
CONTRACTS = Path('/workspace/contracts')

PROMPT = """Implement the Spring Boot service in /workspace/service using the supplied
requirements and OpenAPI design. Generated interfaces and models are the API contract.
Implement real business behavior, validation, persistence and error handling.
Locate code with list_files and search_files and read only the relevant line ranges with
read_file. Create files with write_file; change existing files with edit_file, not by
rewriting them. Use shell for builds and anything else. Add the Spring Boot entry point and any
needed dependencies/configuration. Do not generate, read, or execute tests.
Compile/package with Maven using -Dmaven.test.skip=true. Fix compilation failures.
Create a production Dockerfile at the project root: multi-stage Maven/Java build,
skip tests, non-root runtime, expose 8080 and start the executable Spring Boot jar.
Create .dockerignore excluding build output and tests. Do not put secrets in files.
You have no Docker daemon; the outer worker will build your Dockerfile.
Only finish once implementation, compilation and Dockerfile are complete.
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


def validate_completion(root: Path) -> None:
    if not (root / 'Dockerfile').is_file():
        raise RuntimeError('Agent finished without a Dockerfile')


def main() -> None:
    context = Path('/workspace/input.json').read_text()
    run_agent(AgentProfile(
        root=ROOT, prompt=PROMPT + (REPAIR_PROMPT if json.loads(context).get('repair') else ''),
        initial_messages=[context],
        max_turns_env='IMPLEMENTATION_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Implementation completed',
        exhaustion_message='Implementation exceeded the tool-turn limit',
        contracts=CONTRACTS if CONTRACTS.is_dir() else None,
    ))


if __name__ == '__main__':
    main()
