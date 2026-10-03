"""Contracts-only test authoring policies and trusted standalone runner publication."""
import shutil
from pathlib import Path
from typing import Any

from temporalio import activity

from app.agent_workflows.host import StageInputs, StageSpec, copy_regular_files, run_stage
from app.api_design.openapi import render_openapi
from app.config import GENERATED_CODE_DIR
from app.docker_agents import default_agent_image

TEMPLATE = Path(__file__).parent / 'template'


def copy_test_suite(source: Path, target: Path) -> None:
    """Publish test sources/configuration, excluding links and untrusted runner files."""
    copy_regular_files(source, target, include=lambda relative: (
        relative.parts[:2] == ('src', 'test')
        or relative.as_posix() in {'pom.xml', 'README.md'}
    ))
    # The runner always compiles tests, even if the agent changes its Dockerfile.
    shutil.copyfile(TEMPLATE / 'Dockerfile', target / 'Dockerfile')
    shutil.copyfile(TEMPLATE / '.dockerignore', target / '.dockerignore')
    shutil.copyfile(TEMPLATE / 'run-tests.sh', target / 'run-tests.sh')


def prepare_inputs(session_dir: Path, context: dict[str, Any]) -> StageInputs:
    # No codegen, implementation, image, log or arbitrary extra context is supplied.
    contracts = {
        'requirements': context.get('requirements', []),
        'requirements_document': context.get('requirements_document', ''),
        'api_design': context['api_design'],
    }

    def populate(workspace: Path) -> None:
        shutil.copytree(TEMPLATE, workspace / 'tests')
        (workspace / 'openapi.yaml').write_text(render_openapi(contracts['api_design']))

    return StageInputs(context=contracts, populate=populate)


def validate_output(clean: Path) -> None:
    if not (clean / 'pom.xml').is_file() or not any((clean / 'src/test/java').rglob('*Test.java')):
        raise RuntimeError('Test generation did not produce a Maven test suite')


STAGE = StageSpec(
    role='test_generation', artifact_subdir='tests', workspace_name='tests',
    output_subdir='suite', heartbeat='Generating/compiling service tests', image_label='test runner',
    image_build=default_agent_image('test_generation', 'tests'),
    prepare_inputs=prepare_inputs, sanitize_output=copy_test_suite, validate_output=validate_output,
)


@activity.defn
async def generate_service_tests(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    return await run_stage(STAGE, session_id, context, GENERATED_CODE_DIR)
