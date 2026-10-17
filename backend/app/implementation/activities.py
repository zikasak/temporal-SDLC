"""Implementation policies; shared host machinery builds and publishes the service."""
import dataclasses
import json
import re
from functools import partial
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent_workflows.contracts import on_demand_contracts, write_contracts
from app.agent_workflows.host import (
    StageInputs, StageSpec, copy_regular_files, run_stage, session_language, write_language,
)
from app.config import GENERATED_CODE_DIR
from app.docker_agents import default_agent_image
from app.github.checkout import CHECKOUT_DIR, CONTRACTS_DIR, TESTS_DIR
from app.languages import Language
from app.requirements.document import without_repeated_tables


EXCLUDED = {'.git', '.openapi-generator', '__pycache__', '.agent-output'}
# What a repository holds before a project of its own: a readme, a licence, Git settings.
BARE_REPOSITORY = re.compile(r'(readme|licen[cs]e|copying)(\..*)?|\.git.*|\.github', re.I)


def is_service_file(language: Language, relative: Path) -> bool:
    """Source files only: never tests, build metadata, agent logs or the repository's
    contract tests and published contract."""
    excluded = EXCLUDED | set(language.implementation.test_dirs) | set(language.build_dirs)
    return (relative.parts[0] not in (TESTS_DIR, CONTRACTS_DIR)
            and not any(part.lower() in excluded for part in relative.parts))


def takes_generated_code(language: Language, checkout: Path) -> bool:
    """Whether the generated project fits the repository: it holds no project yet, or
    its service has the generated layout. Into any other project the generated files
    would only bring a second, unused service and its scaffolding."""
    if all((checkout / path).is_dir() for path in language.implementation.contract_paths):
        return True
    return all(BARE_REPOSITORY.fullmatch(item.name) for item in checkout.iterdir())


def copy_service(language: Language, source: Path, target: Path) -> None:
    """Copy regular source files only: never tests, symlinks, build metadata or agent logs."""
    copy_regular_files(source, target, include=partial(is_service_file, language))


def prepare_inputs(language: Language, session_dir: Path, context: dict[str, Any]) -> StageInputs:
    # The author of a session with a repository works in its checkout.
    checkout = session_dir / CHECKOUT_DIR if context.get('repository') else None
    if checkout and not checkout.is_dir():
        raise ApplicationError('The repository checkout is unavailable', non_retryable=True)
    repair = context.get('repair')
    # A change to an existing service generates no code: the checkout alone is edited.
    change = bool(context.get('brief'))
    if change and not checkout:
        raise ApplicationError('The repository checkout is unavailable', non_retryable=True)
    # A repair continues from the published implementation, not the generated skeleton.
    source = (session_dir / 'implementation/service' if repair
              else checkout if change else session_dir / 'server')
    if not source.is_dir():
        raise ApplicationError('Generated server code is unavailable', non_retryable=True)

    generated = bool(checkout) and not change and takes_generated_code(language, checkout)
    if checkout and not change:
        # The author is told whether the generated contract was added to the checkout.
        context = {**context, 'generated_code': generated}
    context = without_repeated_tables(context)
    index, files = on_demand_contracts(context, len(json.dumps(context))) or (context, {})
    contract_paths = language.implementation.contract_paths

    def populate(workspace: Path) -> None:
        project = workspace / 'service'
        if checkout and not repair and not change:
            copy_service(language, checkout, project)
            if generated:
                # Generated sources are the contract and replace the repository's; any
                # other generated file only fills a gap, so an existing build file is kept.
                copy_regular_files(source, project, include=lambda relative: (
                    is_service_file(language, relative)
                    and (relative.parts[0] in contract_paths
                         or not (project / relative).exists())))
        else:
            copy_service(language, source, project)
        write_language(workspace, language,
                       instructions=language.implementation.instructions)
        if files:
            write_contracts(workspace, files)

    return StageInputs(context=index, populate=populate)


def validate_output(clean: Path) -> None:
    if not (clean / 'Dockerfile').is_file():
        raise RuntimeError('Implementation did not produce a Dockerfile')


def stage(language: Language) -> StageSpec:
    return StageSpec(
        role='implementation', artifact_subdir='implementation', workspace_name='service',
        output_subdir='service', heartbeat='Implementing/building service',
        image_label='service',
        image_build=dataclasses.replace(default_agent_image('implementation', 'service'),
                                        dockerfile=language.agent_dockerfile),
        prepare_inputs=partial(prepare_inputs, language),
        sanitize_output=partial(copy_service, language), validate_output=validate_output,
    )


@activity.defn
async def implement_service(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    language = session_language(context.get('language'))
    return await run_stage(stage(language), session_id, context, GENERATED_CODE_DIR)
