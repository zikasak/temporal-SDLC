"""Implementation policies; shared host machinery builds and publishes the service."""
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent_workflows.host import StageInputs, StageSpec, copy_regular_files, run_stage
from app.config import GENERATED_CODE_DIR
from app.docker_agents import default_agent_image


def copy_service(source: Path, target: Path) -> None:
    """Copy regular source files only: never tests, symlinks or build metadata."""
    excluded = {'test', 'tests', 'target', '.git', '.openapi-generator', '__pycache__'}
    copy_regular_files(source, target,
                       include=lambda relative: not any(part.lower() in excluded
                                                        for part in relative.parts))


def prepare_inputs(session_dir: Path, context: dict[str, Any]) -> StageInputs:
    source = session_dir / 'server'
    if not source.is_dir():
        raise ApplicationError('Generated server code is unavailable', non_retryable=True)

    def populate(workspace: Path) -> None:
        copy_service(source, workspace / 'service')

    return StageInputs(context=context, populate=populate)


def validate_output(clean: Path) -> None:
    if not (clean / 'Dockerfile').is_file():
        raise RuntimeError('Implementation did not produce a Dockerfile')


STAGE = StageSpec(
    role='implementation', artifact_subdir='implementation', workspace_name='service',
    output_subdir='service', heartbeat='Implementing/building service', image_label='service',
    image_build=default_agent_image('implementation', 'service'),
    prepare_inputs=prepare_inputs, sanitize_output=copy_service, validate_output=validate_output,
)


@activity.defn
async def implement_service(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    return await run_stage(STAGE, session_id, context, GENERATED_CODE_DIR)
