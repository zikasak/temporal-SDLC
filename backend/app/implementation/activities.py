"""Implementation policies; shared host machinery builds and publishes the service."""
import dataclasses
import json
import re
import tempfile
from functools import partial
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app import docker_agents
from app.asyncio_utils import async_context, run_sync
from app.agent_workflows.contracts import on_demand_contracts, write_contracts
from app.settings.models import DEFAULT_CONTRACT_EMBED_CHARS
from app.agent_workflows.host import (
    StageInputs, StageSpec, WorkTarget, build_image, copy_regular_files, run_stage,
    session_language, write_language,
)
from app.agent_workflows.parts import repository_parts
from app.config import GENERATED_CODE_DIR
from app.docker_agents import default_agent_image
from app.github.checkout import (
    BROWSER_TESTS_DIR, CHECKOUT_DIR, COMPONENT_TESTS_DIR, CONTRACTS_DIR, TESTS_DIR,
)
from app.languages import Language
from app.requirements.document import without_repeated_tables
from app.routing.models import SEAM_ERROR
from app.routing.parts import check_parts, read_routed_parts


EXCLUDED = {'.git', '.openapi-generator', '__pycache__', '.agent-output'}
# Where the code with the seam a change adds before it reproduces anything is published,
# in the session's artifacts: what its baseline runs against and its fix starts from.
SEAM_DIR = 'seam'
# What a repository holds before a project of its own: a readme, a licence, Git settings.
BARE_REPOSITORY = re.compile(r'(readme|licen[cs]e|copying)(\..*)?|\.git.*|\.github', re.I)


def is_service_file(language: Language, relative: Path, unit_tests: bool = False) -> bool:
    """Source files only: never build metadata, agent logs or the repository's contract
    tests, published contracts (of every part, under contracts/), component tests, the
    service's at the root and each other part's inside it, and each part's browser tests.
    The project's own unit tests (in the language's test directories) only with
    `unit_tests`."""
    excluded = (EXCLUDED | set(language.build_dirs)
                | (set() if unit_tests else set(language.implementation.test_dirs)))
    return (relative.parts[0] not in (TESTS_DIR, CONTRACTS_DIR)
            and COMPONENT_TESTS_DIR not in relative.parts
            and BROWSER_TESTS_DIR not in relative.parts
            and not any(part.lower() in excluded for part in relative.parts))


def is_unit_test_file(language: Language, relative: Path) -> bool:
    """A file of the project's own unit tests: one is_service_file shows only with them."""
    return (is_service_file(language, relative, unit_tests=True)
            and not is_service_file(language, relative))


def has_unit_tests(language: Language, tree: Path) -> bool:
    """Whether a service tree carries the project's unit tests: a tree published before
    authors maintained them carries none, and must not remove the repository's."""
    if not tree.is_dir():
        return False
    return any(item.is_file() and not item.is_symlink()
               and is_unit_test_file(language, item.relative_to(tree))
               for item in tree.rglob('*'))


def published_files(language: Language, service: Path):
    """The filter of the repository files that the published `service` tree replaces:
    with the unit tests when it carries them."""
    return partial(is_service_file, language, unit_tests=has_unit_tests(language, service))


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


def copy_service(language: Language, source: Path, target: Path,
                 unit_tests: bool = False) -> None:
    """Copy regular source files only: never symlinks, build metadata or agent logs, and the
    project's own unit tests only with `unit_tests`."""
    copy_regular_files(source, target,
                       include=partial(is_service_file, language, unit_tests=unit_tests))


def copy_project(language: Language, source: Path, target: Path) -> None:
    """An implementation author's output: its sources with the unit tests it maintains."""
    copy_service(language, source, target, unit_tests=True)


def prepare_inputs(language: Language, session_dir: Path, context: dict[str, Any], *,
                   embed_chars: int = DEFAULT_CONTRACT_EMBED_CHARS) -> StageInputs:
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
    # A repair continues from the published implementation, not the generated skeleton,
    # and a change whose seam was added first from the code with that seam; a correction
    # of the seam itself, after its review, from the seam as it was added.
    source = (session_dir / SEAM_DIR / 'service' if repair and context.get('seam')
              else session_dir / 'implementation/service' if repair
              else session_dir / SEAM_DIR / 'service' if change and context.get('seam_prepared')
              else checkout if change else session_dir / 'server')
    # A change has a checkout: one without was refused above.
    if source is None or not source.is_dir():
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
    index, files = on_demand_contracts(context, len(json.dumps(context)), embed_chars) or (context, {})
    contract_paths = language.implementation.contract_paths
    # A change with criteria routed to seams of the code keeps their stubs' signatures,
    # in each part with such criteria.
    stubs = (_routed_contracts(language, session_dir) if context.get('component_routes')
             else [])

    # An author in a checkout maintains the repository's own unit tests with the code; an
    # onboarding's author leaves the existing code, tests included, as it is.
    unit_tests = bool(checkout) and not context.get('onboarding')

    def populate(workspace: Path) -> None:
        project = workspace / 'service'
        if checkout and not repair and not change:
            copy_service(language, checkout, project, unit_tests=unit_tests)
            if generated:
                # Generated sources are the contract and replace the repository's; any
                # other generated file only fills a gap, so an existing build file is kept.
                copy_regular_files(source, project, include=lambda relative: (
                    is_service_file(language, relative)
                    and (relative.parts[0] in contract_paths
                         or not (project / relative).exists())))
        else:
            copy_service(language, source, project, unit_tests=unit_tests)
            if (unit_tests and checkout and source != checkout
                    and not has_unit_tests(language, source)):
                # A tree published before its authors maintained the unit tests: they
                # are the repository's, as the checkout holds them.
                copy_regular_files(checkout, project, include=partial(
                    is_unit_test_file, language))
        write_language(workspace, language,
                       instructions=language.implementation.instructions)
        if files:
            write_contracts(workspace, files)
        for contract, where in stubs:
            # Read-only beside the checkout, as the contracts are; never published from here.
            copy_regular_files(contract, workspace / where, include=lambda relative: True)

    return StageInputs(context=index, populate=populate)


def _routed_contracts(language: Language, session_dir: Path) -> list[tuple[Path, Path]]:
    """The component contract the routing checked of each part with routes, and where an
    author's workspace holds it."""
    try:
        parts = read_routed_parts(language, session_dir)
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    contracts = [(part.routed_contract(session_dir), Path(part.workspace_contract()))
                 for part in parts]
    if not contracts or not all(contract.is_dir() for contract, _ in contracts):
        raise ApplicationError('The component contract of the routing is unavailable',
                               non_retryable=True)
    return contracts


def validate_output(clean: Path) -> None:
    if not (clean / 'Dockerfile').is_file():
        raise RuntimeError('Implementation did not produce a Dockerfile')


def stage(language: Language, artifact_subdir: str = 'implementation') -> StageSpec:
    return StageSpec(
        role='implementation', artifact_subdir=artifact_subdir, workspace_name='service',
        output_subdir='service', heartbeat='Implementing/building service',
        image_label='service',
        image_build=dataclasses.replace(default_agent_image('implementation', 'service'),
                                        dockerfile=language.agent_dockerfile),
        prepare_inputs=partial(prepare_inputs, language),
        sanitize_output=partial(copy_project, language), validate_output=validate_output,
        work=partial(_work_target, language, artifact_subdir),
    )


def _work_target(language: Language, artifact_subdir: str,
                 context: dict[str, Any]) -> WorkTarget | None:
    """The implementation's place in the repository: the whole checkout, as published."""
    if not context.get('repository'):
        return None
    return WorkTarget(stage=artifact_subdir.replace('/', '-'),
                      include=partial(is_service_file, language, unit_tests=True))


# What an onboarding's author writes of its own: the root Dockerfile, its ignore file and
# the repository's checks script.
CHECKS_SCRIPT = Path('.sdlc/checks.sh')
ONBOARDING_FILES = (Path('Dockerfile'), Path('.dockerignore'), CHECKS_SCRIPT)


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
        elif relative == Path('.dockerignore') and (clean / relative).is_file():
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


def seam_error(message: str) -> ApplicationError:
    """A failure of adding the seam, worded by the worker so the user is shown it."""
    message = message.strip()
    return ApplicationError(message if message.endswith(('.', ')')) else f'{message}.',
                            type=SEAM_ERROR, non_retryable=True)


def _seam_output(clean: Path) -> None:
    try:
        validate_output(clean)
    except RuntimeError as exc:
        raise seam_error('The seam agent left no Dockerfile at the root of the repository.'
                         ) from exc


async def _build_seam(clean: Path, log_path: Path) -> str:
    try:
        return await build_image(clean, 'service', log_path)
    except RuntimeError as exc:
        # The tail of the build of the repository's own Dockerfile, with no build arguments,
        # as the conformance check's build failures are shown.
        raise seam_error('The service image could not be built from the root Dockerfile of '
                         'the code with the seam: '
                         f'{str(exc).removeprefix("Docker build failed: ")[-SEAM_BUILD_CHARS:]}'
                         ) from exc


# How much of a failed image build of the code with the seam the user is shown.
SEAM_BUILD_CHARS = 600


async def prepare_seam(language: Language, session_id: str,
                       context: dict[str, Any]) -> dict[str, Any]:
    """Add the seam a change's criteria are observed at, which the code does not have yet
    (`seam`: the symbols to add, by part and module), with no behaviour change, and check
    the code with it against the whole component contract of every part with routes.
    Returns the stage result, its `path` the code with the seam, or the
    `conformance_error` that says how it misses the contract. Any other failure is a
    SEAM_ERROR naming the step that failed."""
    if not context.get('brief') or not context.get('component_routes'):
        raise seam_error('Only a change with criteria routed to a seam adds one.')
    session_dir = GENERATED_CODE_DIR / session_id
    try:
        parts = await run_sync(read_routed_parts, language, session_dir)
    except ValueError as exc:
        raise seam_error(str(exc)) from exc
    for part in parts:
        if part.browser:
            if part.browser_contract is None:
                raise seam_error(f'{part.language.label} has no page contracts.')
        elif part.contract is None:
            raise seam_error(f'{part.language.label} has no component contracts.')
    spec = dataclasses.replace(stage(language, SEAM_DIR), validate_output=_seam_output,
                               build_output=_build_seam)
    try:
        result = await run_stage(spec, session_id, context, GENERATED_CODE_DIR)
    except ApplicationError as exc:
        if exc.type == SEAM_ERROR:
            raise
        # The worker's own: the inputs the agent needs are unavailable.
        raise seam_error(exc.message) from exc
    except Exception as exc:
        # What the agent or its provider said is in the stage's agent.log, not shown.
        raise seam_error(f'The seam agent did not finish ({type(exc).__name__}): what it said '
                         'is in agent.log of the session\'s seam stage.') from exc
    if 'questions' in result or 'provider_limit' in result:
        return result
    try:
        async with docker_agents.keep_activity_alive('Checking the seam against its contract'):
            async with async_context(tempfile.TemporaryDirectory,
                                     dir=session_dir / SEAM_DIR) as temporary:
                # Every part's contract, not only those the seam is in: the seam changes
                # nothing else.
                error = await check_parts(parts, session_dir, Path(result['path']),
                                          Path(temporary), session_dir / SEAM_DIR)
    except Exception as exc:
        raise seam_error('The code with the seam could not be checked against its component '
                         f'contract ({type(exc).__name__}).') from exc
    if error:
        return {'session_id': session_id, 'conformance_error': error}
    return result


@activity.defn
async def implement_service(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    language = session_language(context.get('language'))
    if context.get('seam'):
        return await prepare_seam(language, session_id, context)
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
    if 'questions' in result or 'provider_limit' in result:
        return result
    changed = await run_sync(existing_code_changed, language, checkout, Path(result['path']))
    return {**result, 'code_changed': changed}
