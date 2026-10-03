"""Container-only coding agent. No Temporal, database, or Docker access."""
from pathlib import Path

from app.agent_workflows.runtime import AgentProfile, run_agent

ROOT = Path('/workspace/service')

PROMPT = """Implement the Spring Boot service in /workspace/service using the supplied
requirements and OpenAPI design. Generated interfaces and models are the API contract.
Use read_file, write_file and shell to inspect and implement real business behavior,
validation, persistence and error handling. Add the Spring Boot entry point and any
needed dependencies/configuration. Do not generate, read, or execute tests.
Compile/package with Maven using -Dmaven.test.skip=true. Fix compilation failures.
Create a production Dockerfile at the project root: multi-stage Maven/Java build,
skip tests, non-root runtime, expose 8080 and start the executable Spring Boot jar.
Create .dockerignore excluding build output and tests. Do not put secrets in files.
You have no Docker daemon; the outer worker will build your Dockerfile.
Only finish once implementation, compilation and Dockerfile are complete.
"""


def validate_completion(root: Path) -> None:
    if not (root / 'Dockerfile').is_file():
        raise RuntimeError('Agent finished without a Dockerfile')


def main() -> None:
    run_agent(AgentProfile(
        root=ROOT, prompt=PROMPT,
        initial_messages=[Path('/workspace/input.json').read_text()],
        max_turns_env='IMPLEMENTATION_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Implementation completed',
        exhaustion_message='Implementation exceeded the tool-turn limit',
    ))


if __name__ == '__main__':
    main()
