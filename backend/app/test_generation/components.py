"""The second test author of a change: component tests of the criteria the routing sent
to a seam of the code, written from the component contract's stubs alone and built, by
the trusted runner of the language, with the service's sources the author never sees."""
import dataclasses
import json
import shutil
from functools import partial
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent_workflows.host import (
    StageInputs, StageSpec, build_image, copy_file, copy_regular_files, run_stage,
    session_language, write_language,
)
from app.asyncio_utils import run_sync
from app.config import GENERATED_CODE_DIR
from app.docker_agents import default_agent_image
from app.github.checkout import COMPONENT_SNAPSHOT, SNAPSHOT_DIR
from app.languages import RUNTIME_FILE, ComponentSuite, Language
from app.requirements.document import without_repeated_tables
from app.review.activities import record_baseline
from app.routing.components import COMPONENTS_DIR
from app.routing.models import read_seam
from app.test_generation.activities import copy_test_suite, read_unobservable, validate_output

# Where the routing kept the component contract it checked against the code, in the
# session's artifacts, and where the author reads it, below its read-only contracts.
ROUTED_CONTRACT = Path('routing') / COMPONENTS_DIR
WORKSPACE_CONTRACT = Path('contracts') / COMPONENTS_DIR
ARTIFACT_DIR = 'component-tests'
# The parts of the runner's build context around the trusted runner files.
CONTEXT_REPOSITORY = 'repository'
CONTEXT_SUITE = 'suite'


def component_routes(routes: Any) -> dict[str, dict[str, Any]]:
    """The `component` routes of a delivery's routing, each with its reason and seam."""
    if not isinstance(routes, dict):
        return {}
    return {requirement: {'route': 'component', 'reason': str(route.get('reason') or ''),
                          'seam': read_seam(requirement, route.get('seam'))}
            for requirement, route in routes.items()
            if isinstance(route, dict) and route.get('route') == 'component'}


def _contract(session_dir: Path) -> Path:
    contract = session_dir / ROUTED_CONTRACT
    if not (contract / RUNTIME_FILE).is_file():
        raise ApplicationError('The component contract of the routing is unavailable',
                               non_retryable=True)
    return contract


def prepare_inputs(language: Language, session_dir: Path, context: dict[str, Any]) -> StageInputs:
    suite = language.component_suite
    contract = _contract(session_dir)
    # No service source, image or log, and not the HTTP contract: the brief, the criteria
    # this suite proves with their seams, and the stubs of those seams.
    contracts = without_repeated_tables({
        'component': True,
        'brief': context['brief'],
        'routes': context['routes'],
        'requirements': context.get('requirements', []),
        'requirements_document': context.get('requirements_document', ''),
        **({'architecture': context['architecture']} if context.get('architecture') else {}),
        **({'repository': context['repository']} if context.get('repository') else {}),
    })
    snapshot = session_dir / SNAPSHOT_DIR / COMPONENT_SNAPSHOT
    if context.get('repository') and not snapshot.is_dir():
        raise ApplicationError('The repository tests were not recorded', non_retryable=True)

    def populate(workspace: Path) -> None:
        copy_test_suite(suite, suite.template, workspace / 'tests')
        if context.get('repository'):
            # The repository's own component tests, which the author continues.
            copy_test_suite(suite, snapshot, workspace / 'tests')
        # Validated by the routing: regular files only.
        shutil.copytree(contract, workspace / WORKSPACE_CONTRACT)
        write_language(workspace, language, instructions=suite.instructions,
                       test_glob=suite.test_glob)

    return StageInputs(context=contracts, populate=populate)


def runner_context(suite: ComponentSuite, repository: Path, tests: Path, context: Path) -> None:
    """The build context of the runner: its trusted files from the template, the service's
    sources and the sanitized suite."""
    context.mkdir()
    for name in suite.runner:
        copy_file(suite.template / name, context / name)
    copy_regular_files(repository, context / CONTEXT_REPOSITORY, include=lambda relative: True)
    shutil.copytree(tests, context / CONTEXT_SUITE)


async def build_runner(language: Language, session_dir: Path, tests: Path,
                       log_path: Path) -> str:
    """Build the suite's runner with the service's sources as the delivery started from
    them: the build collects the suite, so a test that does not even import fails here."""
    suite, contract = language.component_suite, language.component_contract
    repository = await run_sync(record_baseline, language, session_dir)
    text = await run_sync((_contract(session_dir) / RUNTIME_FILE).read_text)
    try:
        runtime = contract.read_runtime(json.loads(text), repository)
    except ValueError as exc:
        raise RuntimeError(f'The component contract\'s {RUNTIME_FILE} is unusable: '
                           f'{exc}') from exc
    context = tests.parent / 'runner'
    await run_sync(runner_context, suite, repository, tests, context)
    return await build_image(context, 'component test runner', log_path,
                             build_args=suite.build_args(runtime))


def stage(language: Language, session_dir: Path, frozen: dict[str, str],
          requirement_ids: list[str]) -> StageSpec:
    """`frozen` holds the repository's component tests, which the author must leave as they
    are, and `requirement_ids` the criteria it may name as unobservable instead."""
    suite = language.component_suite
    return StageSpec(
        role='test_generation', artifact_subdir=ARTIFACT_DIR, workspace_name='tests',
        output_subdir='suite', heartbeat='Generating/collecting component tests',
        image_label='component test runner',
        image_build=dataclasses.replace(default_agent_image('test_generation', 'tests'),
                                        dockerfile=language.agent_dockerfile),
        prepare_inputs=partial(prepare_inputs, language),
        sanitize_output=partial(copy_test_suite, suite),
        validate_output=partial(validate_output, suite, frozen, True),
        declined_result=partial(read_unobservable, requirement_ids),
        build_output=partial(build_runner, language, session_dir),
    )


@activity.defn
async def generate_component_tests(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    """Regression tests of a change's criteria routed to a seam, added to the repository's
    component tests. Like the contract tests' first author, the author may instead name
    criteria it finds no test of the seam can observe (`unobservable`)."""
    language = session_language(context.get('language'))
    if language.component_suite is None or language.component_contract is None:
        raise ApplicationError(f'{language.label} has no component tests', non_retryable=True)
    if not context.get('brief') or context.get('repair'):
        # Diagnosis and repair of component tests are step 5 of
        # plans/component-contracts.md.
        raise ApplicationError('Component tests are written only for the first run of a '
                               "change's tests", non_retryable=True)
    try:
        routes = component_routes(context.get('routes'))
    except RuntimeError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    if not routes:
        raise ApplicationError('No criterion is routed to the component tests',
                               non_retryable=True)
    context = {**context, 'routes': routes}
    suite = language.component_suite
    session_dir = GENERATED_CODE_DIR / session_id
    snapshot = session_dir / SNAPSHOT_DIR / COMPONENT_SNAPSHOT
    continued = bool(context.get('repository'))
    repository = await run_sync(suite.test_cases, snapshot) if continued else {}
    result = await run_stage(stage(language, session_dir, repository, list(routes)),
                             session_id, context, GENERATED_CODE_DIR)
    if 'questions' in result or 'unobservable' in result:
        return result
    cases = await run_sync(suite.test_cases, Path(result['path']))
    return {**result,
            'added_tests': sorted(cases.keys() - repository.keys()),
            'changed_tests': sorted(case for case, source in repository.items()
                                    if case in cases and cases[case] != source),
            'removed_tests': sorted(repository.keys() - cases.keys())}
