"""Contracts-only test authoring policies and trusted standalone runner publication."""
import dataclasses
import json
import shutil
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
from app.api_design.openapi import render_openapi
from app.config import GENERATED_CODE_DIR
from app.docker_agents import default_agent_image
from app.github.checkout import SNAPSHOT_DIR
from app.languages import ComponentSuite, Language, TestSuite
from app.requirements.document import without_repeated_tables
from app.routing.models import UNOBSERVABLE_FILE, read_routes

MAX_NAMED_TESTS = 5
MAX_UNOBSERVABLE_BYTES = 256 * 1024


def copy_test_suite(suite: TestSuite, source: Path, target: Path) -> None:
    """Publish test sources/configuration, excluding links and untrusted runner files."""
    published = {Path(item) for item in suite.published}
    copy_regular_files(source, target, include=lambda relative: (
        relative in published or not published.isdisjoint(relative.parents)
    ))
    if isinstance(suite, ComponentSuite):
        # Its runner is put around it, with the code it tests, only when it is built.
        return
    # The runner always compiles tests, even if the agent changes its Dockerfile; it is
    # supplied by the language's template and never comes from the test author.
    for name in suite.runner:
        shutil.copyfile(suite.template / name, target / name)


def prepare_inputs(language: Language, session_dir: Path, context: dict[str, Any]) -> StageInputs:
    suite = language.test_suite
    # No codegen, implementation, image, log or arbitrary extra context is supplied.
    api_design = context['api_design']
    # The rendered OpenAPI file is the agent's API authority; the structured design
    # it is rendered from would only repeat it in every model invocation.
    contracts = without_repeated_tables({
        'requirements': context.get('requirements', []),
        'requirements_document': context.get('requirements_document', ''),
        # A change to an existing service adds tests for its brief to the existing suite,
        # for the criteria routed to it when another suite proves some.
        **({'brief': context['brief']} if context.get('brief') else {}),
        **({'routes': context['routes']} if context.get('routes') else {}),
        **({'architecture': context['architecture']} if context.get('architecture') else {}),
    })
    repair = context.get('repair')
    previous = session_dir / 'tests/suite'
    if repair:
        if not previous.is_dir():
            raise ApplicationError('Generated tests are unavailable', non_retryable=True)
        contracts['repair'] = repair
    if context.get('keep'):
        # The repository's tests the changed service still passes, with their sources.
        contracts['keep'] = context['keep']

    # The author of a session with a repository continues that repository's contract
    # tests, as the delivery recorded them before any author ran; the rest of the
    # checkout stays out of its workspace.
    repository = context.get('repository')
    snapshot = session_dir / SNAPSHOT_DIR / 'suite'
    # An onboarding writes the contract tests anew, replacing any the repository has.
    onboarding = bool(context.get('onboarding'))
    if onboarding:
        contracts['onboarding'] = True
    if repository and not onboarding:
        if not snapshot.is_dir():
            raise ApplicationError('The repository tests were not recorded', non_retryable=True)
        contracts['repository'] = repository

    openapi = render_openapi(api_design)
    index, files = on_demand_contracts({**contracts, 'api_design': api_design},
                                       len(json.dumps(contracts)) + len(openapi)) or (contracts, {})

    def populate(workspace: Path) -> None:
        shutil.copytree(suite.template, workspace / 'tests')
        if repair:
            # A repair continues from the published suite; runner files stay the template's.
            copy_test_suite(suite, previous, workspace / 'tests')
        elif repository and not onboarding:
            copy_test_suite(suite, snapshot, workspace / 'tests')
        write_language(workspace, language, instructions=suite.instructions,
                       test_glob=suite.test_glob)
        if files:
            write_contracts(workspace, files)
        else:
            (workspace / 'openapi.yaml').write_text(openapi)

    return StageInputs(context=index, populate=populate)


def validate_output(suite: TestSuite, frozen: dict[str, str] | None, must_add: bool,
                    clean: Path) -> None:
    if (not all((clean / name).is_file() for name in suite.required)
            or not any(clean.glob(suite.test_glob))):
        raise RuntimeError('Test generation did not produce a test suite')
    untracked = suite.untracked_tests(clean)
    if untracked:
        raise RuntimeError('Test generation wrote tests that cannot be tracked: '
                           + '; '.join(untracked[:MAX_NAMED_TESTS]))
    if frozen is None:
        return
    cases = suite.test_cases(clean)
    changed = sorted(case for case, source in frozen.items() if cases.get(case) != source)
    if changed:
        raise RuntimeError('Test generation changed or removed tests it may not touch: '
                           + ', '.join(changed[:MAX_NAMED_TESTS]))
    if must_add and not cases.keys() - frozen.keys():
        raise RuntimeError('Test generation did not add a regression test')


def read_unobservable(requirement_ids: list[str], output: Path) -> dict[str, Any] | None:
    """The criteria of the brief the author found no test of the suite can observe, as
    `unobservable`, when it wrote UNOBSERVABLE_FILE instead of a suite. The file is
    untrusted: it has to name criteria of the brief, each with its reason."""
    file = output / UNOBSERVABLE_FILE
    if file.is_symlink() or not file.exists():
        return None
    if not file.is_file() or file.stat().st_size > MAX_UNOBSERVABLE_BYTES:
        raise RuntimeError(f'Test generation wrote an unreadable {UNOBSERVABLE_FILE}')
    try:
        data = json.loads(file.read_text())
    except ValueError as exc:
        raise RuntimeError(f'{UNOBSERVABLE_FILE} is not valid JSON') from exc
    items = data.get('unobservable') if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise RuntimeError(f'{UNOBSERVABLE_FILE} names no criterion')
    named = {item.get('requirement_id') for item in items if isinstance(item, dict)}
    # The same checks as a routing's, but only of the criteria the file names.
    routes = read_routes(
        {'routes': [{**item, 'route': 'unobservable'} if isinstance(item, dict) else item
                    for item in items]},
        [requirement for requirement in requirement_ids if requirement in named])
    return {'unobservable': [{'requirement_id': requirement, 'reason': route['reason']}
                             for requirement, route in routes.items()]}


def stage(language: Language, frozen: dict[str, str] | None = None,
          must_add: bool = False, requirement_ids: list[str] | None = None) -> StageSpec:
    """`frozen` holds the tests, by identity, whose source the author must leave as it is.
    `requirement_ids` are the criteria of a brief the author may name as unobservable
    instead of writing a suite; None when it may not."""
    suite = language.test_suite
    return StageSpec(
        role='test_generation', artifact_subdir='tests', workspace_name='tests',
        output_subdir='suite', heartbeat='Generating/compiling service tests',
        image_label='test runner',
        image_build=dataclasses.replace(default_agent_image('test_generation', 'tests'),
                                        dockerfile=language.agent_dockerfile),
        prepare_inputs=partial(prepare_inputs, language),
        sanitize_output=partial(copy_test_suite, suite),
        validate_output=partial(validate_output, suite, frozen, must_add),
        declined_result=(partial(read_unobservable, requirement_ids)
                         if requirement_ids is not None else None),
    )


@activity.defn
async def generate_service_tests(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    language = session_language(context.get('language'))
    suite = language.test_suite
    session_dir = GENERATED_CODE_DIR / session_id
    repair = context.get('repair')
    change = bool(context.get('brief'))
    # The repository's tests as they were before any author ran. A change that leaves the
    # API as it is adds to them and leaves them alone.
    continued = bool(context.get('repository')) and not context.get('onboarding')
    repository = (
        await run_sync(suite.test_cases, session_dir / SNAPSHOT_DIR / 'suite') if continued else {}
    )
    frozen = None
    if repair and repair.get('review'):
        # Repository tests the suite has to put back: the author gets each one's source,
        # and a suite in which one of them still differs from it is rejected.
        restore = {case: repository[case] for case in repair['review'].get('restore', [])
                   if case in repository}
        # Code review findings concern the delivery's own tests: the repository's tests
        # it left as they were stay so for a change, and while others are put back.
        if change or restore:
            published = await run_sync(suite.test_cases, session_dir / 'tests/suite')
            frozen = {case: source for case, source in repository.items()
                      if published.get(case) == source}
        if restore:
            frozen = {**(frozen or {}), **restore}
            context = {**context, 'repair': {
                **repair, 'review': {**repair['review'], 'restore': restore}}}
    elif repair:
        # Only the tests the diagnosis found broken may differ from the published suite.
        broken = {suite.case_id(item['class_name'], item['name'])
                  for item in repair.get('broken_tests', [])}
        frozen = {
            case: source
            for case, source in (
                await run_sync(suite.test_cases, session_dir / 'tests/suite')
            ).items()
            if case not in broken
        }
    elif change:
        frozen = repository
    # Repository tests the changed service was seen to pass as they are stay so in every
    # authoring run, a repair that found one of them failing included.
    keep = {case: repository[case] for case in context.get('keep', []) if case in repository}
    if keep:
        frozen = {**(frozen or {}), **keep}
    context = {**context, 'keep': keep} if keep else {
        key: value for key, value in context.items() if key != 'keep'}
    # The first author of a change's tests may find a criterion of its brief that no test
    # of the suite can observe, and say so instead of writing a suite: one of the criteria
    # routed to the suite, when the routing sent others elsewhere.
    requirement_ids = ([item['id'] for item in context['brief']['requirements']
                        if not context.get('routes') or item['id'] in context['routes']]
                       if change and not repair else None)
    result = await run_stage(stage(language, frozen, must_add=change and not repair,
                                   requirement_ids=requirement_ids),
                             session_id, context, GENERATED_CODE_DIR)
    if not continued or 'questions' in result or 'unobservable' in result:
        return result
    # What the suite now differs in from the repository's, for the runs that judge it.
    cases = await run_sync(suite.test_cases, Path(result['path']))
    return {**result,
            'added_tests': sorted(cases.keys() - repository.keys()),
            'changed_tests': sorted(case for case, source in repository.items()
                                    if case in cases and cases[case] != source),
            'removed_tests': sorted(repository.keys() - cases.keys())}
