"""Implementation policies; shared host machinery builds and publishes the service."""
import dataclasses
import json
import re
from functools import partial
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.asyncio_utils import run_sync
from app.agent_workflows.contracts import on_demand_contracts, write_contracts
from app.agent_workflows.host import (
    StageInputs, StageSpec, copy_regular_files, run_stage, session_language, write_language,
)
from app.agent_workflows.parts import repository_parts
from app.config import GENERATED_CODE_DIR
from app.docker_agents import default_agent_image
from app.github.checkout import CHECKOUT_DIR, COMPONENT_TESTS_DIR, CONTRACTS_DIR, TESTS_DIR
from app.languages import Language
from app.requirements.document import without_repeated_tables
from app.routing.components import COMPONENTS_DIR, ROUTED_CONTRACT


EXCLUDED = {'.git', '.openapi-generator', '__pycache__', '.agent-output'}
# What a repository holds before a project of its own: a readme, a licence, Git settings.
BARE_REPOSITORY = re.compile(r'(readme|licen[cs]e|copying)(\..*)?|\.git.*|\.github', re.I)


def is_service_file(language: Language, relative: Path) -> bool:
    """Source files only: never tests, build metadata, agent logs or the repository's
    contract and component tests and published contract."""
    excluded = EXCLUDED | set(language.implementation.test_dirs) | set(language.build_dirs)
    return (relative.parts[0] not in (TESTS_DIR, COMPONENT_TESTS_DIR, CONTRACTS_DIR)
            and not any(part.lower() in excluded for part in relative.parts))


def takes_generated_code(language: Language, checkout: Path) -> bool:
    """Whether the generated project fits the repository: it holds no project yet, or
    its service has the generated layout. Into any other project the generated files
    would only bring a second, unused service and its scaffolding."""
    if all((checkout / path).is_dir() for path in language.implementation.contract_paths):
        return True
    return all(BARE_REPOSITORY.fullmatch(item.name) for item in checkout.iterdir())


def checkout_parts(language: Language, checkout: Path) -> list[dict[str, Any]]:
    """The projects of a checkout its authors work in, when it holds more than one: the
    service and the others beside it, such as a web frontend in another language."""
    parts = repository_parts(checkout, partial(is_service_file, language))
    return parts if len(parts) > 1 else []


def copy_service(language: Language, source: Path, target: Path) -> None:
    """Copy regular source files only: never tests, symlinks, build metadata or agent logs."""
    copy_regular_files(source, target, include=partial(is_service_file, language))


def prepare_inputs(language: Language, session_dir: Path, context: dict[str, Any]) -> StageInputs:
    # The author of a session with a repository works in its checkout.
    checkout = session_dir / CHECKOUT_DIR if context.get('repository') else None
    if checkout and not checkout.is_dir():
        raise ApplicationError('The repository checkout is unavailable', non_retryable=True)
    repair = context.get('repair')
    # A change to an existing service generates no code: the checkout alone is edited. Nor
    # does an onboarding, whose author writes the Dockerfile of the existing code.
    change = bool(context.get('brief')) or bool(context.get('onboarding'))
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
    # The task covers every project of the repository, not only the service; an
    # onboarding's author writes the service's Dockerfile alone.
    parts = (checkout_parts(language, checkout)
             if checkout and not context.get('onboarding') else [])
    if parts:
        context = {**context, 'repository_parts': parts}
    context = without_repeated_tables(context)
    index, files = on_demand_contracts(context, len(json.dumps(context))) or (context, {})
    contract_paths = language.implementation.contract_paths
    # A change with criteria routed to seams of the code keeps their stubs' signatures.
    stubs = session_dir / ROUTED_CONTRACT if context.get('component_routes') else None
    if stubs and not stubs.is_dir():
        raise ApplicationError('The component contract of the routing is unavailable',
                               non_retryable=True)

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
        if stubs:
            # Read-only beside the checkout, as the contracts are; never published from here.
            copy_regular_files(stubs, workspace / 'contracts' / COMPONENTS_DIR,
                               include=lambda relative: True)

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


# What an onboarding's author writes of its own: the root Dockerfile and its ignore file.
ONBOARDING_FILES = (Path('Dockerfile'), Path('.dockerignore'))


def code_change_allowed(context: dict[str, Any]) -> bool:
    """Whether an onboarding's author may change the existing code: only to fix a bug the
    user confirmed, or as the user's own instructions ask."""
    repair = context.get('repair') or {}
    return bool(repair.get('code_change') or (repair.get('review') or {}).get('instructions'))


def keep_existing_code(language: Language, source: Path, output: Path, clean: Path) -> None:
    """The onboarding author's output as allowed: the code as `source` holds it, with only
    the Dockerfile and its ignore file taken from what the author wrote."""
    copy_service(language, source, clean)
    for relative in ONBOARDING_FILES:
        written = output / relative
        if written.is_file() and not written.is_symlink():
            copy_regular_files(output, clean, include=lambda item, name=relative: item == name)
        elif relative != Path('Dockerfile') and (clean / relative).is_file():
            (clean / relative).unlink()


def _service_files(language: Language, root: Path) -> dict[Path, bytes]:
    if not root.is_dir():
        return {}
    return {item.relative_to(root): item.read_bytes() for item in root.rglob('*')
            if item.is_file() and not item.is_symlink()
            and is_service_file(language, item.relative_to(root))
            and item.relative_to(root) not in ONBOARDING_FILES}


def existing_code_changed(language: Language, checkout: Path, published: Path) -> bool:
    """Whether the onboarding's service differs from the repository's code apart from the
    Dockerfile and its ignore file."""
    return _service_files(language, checkout) != _service_files(language, published)


@activity.defn
async def implement_service(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    language = session_language(context.get('language'))
    if not context.get('onboarding'):
        return await run_stage(stage(language), session_id, context, GENERATED_CODE_DIR)
    session_dir = GENERATED_CODE_DIR / session_id
    checkout = session_dir / CHECKOUT_DIR
    spec = stage(language)
    if not code_change_allowed(context):
        source = (session_dir / 'implementation/service' if context.get('repair')
                  else checkout)
        spec = dataclasses.replace(
            spec, sanitize_output=lambda output, clean: keep_existing_code(
                language, source, output, clean))
    result = await run_stage(spec, session_id, context, GENERATED_CODE_DIR)
    if 'questions' in result:
        return result
    changed = await run_sync(existing_code_changed, language, checkout, Path(result['path']))
    return {**result, 'code_changed': changed}
