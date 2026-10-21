"""Container-only documentation author. Its filesystem holds documentation files only."""
import asyncio
import json
from pathlib import Path

from app.asyncio_utils import run_sync
from app.agent_workflows.runtime import AgentProfile, configure_container_logging, run_agent

ROOT = Path('/workspace/repository')
# Present only when the contracts were too large to embed in input.json.
CONTRACTS = Path('/workspace/contracts')

# What the host keeps of the author's output, written by the host.
LANGUAGE = Path('/workspace/language.json')

PROMPT = """Change the documentation in /workspace/repository. It holds the documentation
files of the project's Git repository, named by the `repository` entry of the input, at
their paths in that repository, and nothing else of it. The `brief` entry of the input is
the task: which documentation should change, what it should say afterwards and how to
tell the change is complete.
The supplied requirements and OpenAPI design are the contract the service was built from
and the only authority on what it does. Describe the service as they do. Do not invent
behaviour, commands, configuration or file names that neither the contract, the brief nor
the existing documentation states.
Read the documentation the brief concerns before changing anything. Locate text with
list_files and search_files and read only the relevant line ranges with read_file. Change
existing files with edit_file, not by rewriting them; create a file with write_file only
when the brief needs a new one. Follow the structure, tone and markup of the existing
documentation and leave what the brief does not concern as it is.
Only documentation files ({suffixes}) are kept: any other file you write is discarded.
There is no service code, test or build here and none is needed, so do not try to build
or run anything. Do not put secrets in files.
Every file you change is pushed to a new branch of the repository, marked as not
verified. Only finish once the brief is met.
"""


def validate_completion(root: Path) -> None:
    """Nothing to build; the host checks that the documentation changed."""


async def main() -> None:
    context = await run_sync(Path('/workspace/input.json').read_text)
    language = json.loads(await run_sync(LANGUAGE.read_text))
    await run_agent(AgentProfile(
        root=ROOT, prompt=PROMPT.format(suffixes=language['suffixes']),
        initial_messages=[context],
        max_turns_env='DOCUMENTATION_MAX_TURNS', validate_completion=validate_completion,
        completion_message='Documentation completed',
        exhaustion_message='Documentation exceeded the tool-turn limit',
        contracts=CONTRACTS if await run_sync(CONTRACTS.is_dir) else None,
        build_dirs=language['build_dirs'],
    ))


if __name__ == '__main__':
    configure_container_logging()
    asyncio.run(main())
