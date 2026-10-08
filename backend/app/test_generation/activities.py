"""Contracts-only test authoring policies and trusted standalone runner publication."""
import json
import shutil
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent_workflows.contracts import on_demand_contracts, write_contracts
from app.agent_workflows.host import StageInputs, StageSpec, copy_regular_files, run_stage
from app.api_design.openapi import render_openapi
from app.config import GENERATED_CODE_DIR
from app.docker_agents import default_agent_image
from app.github.checkout import CHECKOUT_DIR, TESTS_DIR
from app.requirements.document import without_repeated_tables

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
    api_design = context['api_design']
    # The rendered OpenAPI file is the agent's API authority; the structured design
    # it is rendered from would only repeat it in every model invocation.
    contracts = without_repeated_tables({
        'requirements': context.get('requirements', []),
        'requirements_document': context.get('requirements_document', ''),
    })
    repair = context.get('repair')
    previous = session_dir / 'tests/suite'
    if repair:
        if not previous.is_dir():
            raise ApplicationError('Generated tests are unavailable', non_retryable=True)
        contracts['repair'] = repair

    # The author of a session with a repository continues that repository's contract
    # tests; the rest of the checkout stays out of its workspace.
    repository = context.get('repository')
    checkout = session_dir / CHECKOUT_DIR
    if repository:
        if not checkout.is_dir():
            raise ApplicationError('The repository checkout is unavailable', non_retryable=True)
        contracts['repository'] = repository

    openapi = render_openapi(api_design)
    index, files = on_demand_contracts({**contracts, 'api_design': api_design},
                                       len(json.dumps(contracts)) + len(openapi)) or (contracts, {})

    def populate(workspace: Path) -> None:
        shutil.copytree(TEMPLATE, workspace / 'tests')
        if repair:
            # A repair continues from the published suite; runner files stay the template's.
            copy_test_suite(previous, workspace / 'tests')
        elif repository and (checkout / TESTS_DIR).is_dir():
            copy_test_suite(checkout / TESTS_DIR, workspace / 'tests')
        if files:
            write_contracts(workspace, files)
        else:
            (workspace / 'openapi.yaml').write_text(openapi)

    return StageInputs(context=index, populate=populate)


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
