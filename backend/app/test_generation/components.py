"""The second test author of a change: component tests of the criteria the routing sent
to a seam of the code, written from the component contract's stubs alone and built, by
the trusted runner of the language, with the sources the author never sees.

The seams may be in the service or in other parts of the repository, such as a web
client beside it. The suite then has one project per part with routes, and its runner is
put together from each part's own runner (app/languages/composite.py)."""
import dataclasses
import json
import re
import shlex
import shutil
import uuid
from fnmatch import fnmatchcase
from functools import partial
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app import docker_agents
from app.agent_workflows.host import (
    StageInputs, StageSpec, WorkTarget, build_image, copy_file, copy_regular_files,
    restore_stage_output, run_stage, session_language, write_language,
)
from app.asyncio_utils import run_sync
from app.config import GENERATED_CODE_DIR
from app.docker_agents import default_agent_image
from app.github.checkout import COMPONENT_TESTS_DIR, COMPONENT_SNAPSHOT, SNAPSHOT_DIR
from app.implementation.activities import SEAM_DIR
from app.languages import RUNTIME_FILE, TOOLS_DIR, ComponentSuite, Language
from app.languages.composite import CompositeSuite, SuiteMember, suite_members
from app.requirements.document import without_repeated_tables
from app.review.activities import record_baseline
from app.routing.models import read_seam
from app.routing.parts import (
    SERVICE_PART, ComponentPart, component_suite, read_routed_parts,
)
from app.verification.models import COMPONENT_TESTS_ARTIFACT
from app.test_generation.activities import copy_test_suite, read_unobservable, validate_output

ARTIFACT_DIR = COMPONENT_TESTS_ARTIFACT
# The parts of the runner's build context around the trusted runner files.
CONTEXT_REPOSITORY = 'repository'
CONTEXT_SUITE = 'suite'
# Where the composite runner keeps each part's runner tree: /parts/<index>/.
COMPOSITE_PARTS = '/parts'
# The user every runner image runs its tests as.
RUNNER_USER = '10001:10001'
# What the composite runner's tests may take in all.
COMPOSITE_SECONDS = 600


def delivery_parts(language: Language, session_dir: Path) -> list[ComponentPart]:
    """The parts the session's routing routed criteria to seams of, the service first."""
    try:
        return read_routed_parts(language, session_dir)
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc


def delivery_suite(language: Language, session_dir: Path
                   ) -> tuple[list[ComponentPart], ComponentSuite]:
    """The parts with routes and the component suite of their tests."""
    parts = delivery_parts(language, session_dir)
    if not parts:
        raise ApplicationError('No criterion is routed to the component tests',
                               non_retryable=True)
    try:
        return parts, component_suite(language, parts)
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc


def component_routes(routes: Any, parts: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    """The `component` routes of a delivery's routing, each with its reason and seam, at a
    seam of the service or of one of `parts`."""
    if not isinstance(routes, dict):
        return {}
    return {requirement: {'route': 'component', 'reason': str(route.get('reason') or ''),
                          'seam': read_seam(requirement, route.get('seam'), parts)}
            for requirement, route in routes.items()
            if isinstance(route, dict) and route.get('route') == 'component'}


def _contract(session_dir: Path, part: ComponentPart) -> Path:
    contract = part.routed_contract(session_dir)
    if not (contract / RUNTIME_FILE).is_file():
        raise ApplicationError(
            'The component contract of the routing is unavailable' if part.is_service
            else f'The component contract of part {part.name} is unavailable',
            non_retryable=True)
    return contract


def _members(suite: ComponentSuite, parts: list[ComponentPart]
             ) -> list[tuple[ComponentPart, SuiteMember]]:
    """Each member of the suite with the part it tests."""
    by_name = {part.name: part for part in parts}
    return [(by_name[member.name], member) for member in suite_members(suite)]


def copy_template(suite: ComponentSuite, target: Path) -> None:
    """The starting project of the suite: each member's template in its place."""
    for member in suite_members(suite):
        copy_test_suite(member.suite, member.suite.template, target / member.directory)


def prepare_inputs(language: Language, parts: list[ComponentPart], suite: ComponentSuite,
                   session_dir: Path, context: dict[str, Any]) -> StageInputs:
    contracts_of = [(part, _contract(session_dir, part)) for part in parts]
    # No source, image or log, and not the HTTP contract: the brief, the criteria this
    # suite proves with their seams, and the stubs of those seams.
    contracts = without_repeated_tables({
        'component': True,
        'brief': context['brief'],
        'routes': context['routes'],
        'requirements': context.get('requirements', []),
        'requirements_document': context.get('requirements_document', ''),
        **({'architecture': context['architecture']} if context.get('architecture') else {}),
        **({'repository': context['repository']} if context.get('repository') else {}),
        # The parts the suite has a project for, when it is not the service's alone.
        **({'parts': [part.describe() for part in parts]}
           if isinstance(suite, CompositeSuite) else {}),
    })
    snapshot = session_dir / SNAPSHOT_DIR / COMPONENT_SNAPSHOT
    if context.get('repository') and not snapshot.is_dir():
        raise ApplicationError('The repository tests were not recorded', non_retryable=True)
    repair = context.get('repair')
    previous = session_dir / ARTIFACT_DIR / 'suite'
    if repair:
        if not previous.is_dir():
            raise ApplicationError('Generated component tests are unavailable',
                                   non_retryable=True)
        contracts['repair'] = repair

    def populate(workspace: Path) -> None:
        copy_template(suite, workspace / 'tests')
        if repair:
            # A repair continues from the published suite.
            copy_test_suite(suite, previous, workspace / 'tests')
        elif context.get('repository'):
            # The repository's own component tests of each part, which the author
            # continues.
            copy_test_suite(suite, snapshot, workspace / 'tests')
        for part, contract in contracts_of:
            # Validated by the routing: regular files only.
            shutil.copytree(contract, workspace / part.workspace_contract())
        for tool in suite.tools:
            (workspace / TOOLS_DIR).mkdir(exist_ok=True)
            copy_file(tool, workspace / TOOLS_DIR / tool.name)
        write_language(workspace, language, instructions=suite.instructions,
                       test_glob=suite.test_glob,
                       test_globs=[(member.directory / member.suite.test_glob).as_posix()
                                   for member in suite_members(suite)])

    return StageInputs(context=contracts, populate=populate)


def runner_context(suite: ComponentSuite, repository: Path, tests: Path, context: Path) -> None:
    """The build context of a suite's own runner: its trusted files from the template, the
    repository's sources and the sanitized suite."""
    context.mkdir(parents=True)
    for name in suite.runner:
        copy_file(suite.template / name, context / name)
    copy_regular_files(repository, context / CONTEXT_REPOSITORY, include=lambda relative: True)
    copy_test_suite(suite, tests, context / CONTEXT_SUITE)


def _runtime(part: ComponentPart, session_dir: Path, repository: Path) -> dict[str, Any]:
    text = (_contract(session_dir, part) / RUNTIME_FILE).read_text()
    contract = part.contract
    assert contract is not None  # only a part with component tests is routed to
    try:
        return contract.read_runtime(json.loads(text), repository)
    except ValueError as exc:
        raise RuntimeError(f'The component contract\'s {RUNTIME_FILE} is unusable: '
                           f'{exc}') from exc


async def _build_member(part: ComponentPart, suite: ComponentSuite, session_dir: Path,
                        repository: Path, tests: Path, work: Path, log_path: Path) -> str:
    runtime = await run_sync(_runtime, part, session_dir, repository)
    context = work / 'context'
    await run_sync(runner_context, suite, repository, tests, context)
    return await build_image(context, 'component test runner', log_path,
                             build_args=suite.build_args(runtime))


def _slug(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]+', '-', name).strip('-') or 'part'


def part_report(language: Language, index: int, part: ComponentPart) -> str:
    """The name of the report of a part's tests in the composite runner: one the service
    language's report pattern matches."""
    pattern = language.test_reports.pattern
    name = pattern.replace('*', f'-part-{index}-{_slug(part.name)}', 1)
    if '*' in name or not fnmatchcase(name, pattern):
        raise RuntimeError(f'{language.label} test reports are named in a way the component '
                           'tests of other parts cannot follow')
    return name


def composite_files(language: Language, members: list[tuple[ComponentPart, SuiteMember]],
                    images: dict[str, str]) -> dict[str, str]:
    """The Dockerfile and scripts of the composite runner, around the members' runner
    images, each named by `images` (its local tag). The service's runner, when it is a
    member, is the base: its command, as its image runs it, runs the service's tests, and
    each other part's tree is copied in beside it, with what its command runs with."""
    service = next((member for _, member in members if member.service), None)
    others = [(part, member) for part, member in members if not member.service]
    base = images[service.name] if service else images[others[0][1].name]
    reports = language.test_reports.directory
    lines = ['# Generated by the host: the component tests of every part of the change, '
             'run in one image',
             f'FROM {base}']
    commands = []
    for index, (part, member) in enumerate(others):
        root = f'{COMPOSITE_PARTS}/{index}'
        for path in ('/repository', '/tests', '/runner'):
            lines.append(f'COPY --from={images[member.name]} --chown={RUNNER_USER} '
                         f'{path} {root}{path}')
        if service:
            for path in member.suite.part_runtime:
                lines.append(f'COPY --from={images[member.name]} {path} {path}')
            if member.suite.part_runtime:
                # The service's runner holds what the part's runtime loads, such as
                # Node's libstdc++, or the build says so.
                lines.append(f'RUN {shlex.quote(member.suite.part_runtime[0])} --version '
                             f'> /dev/null || {{ echo "The runtime of part {part.name} does '
                             'not start on the service\'s component test runner" >&2; '
                             'exit 1; }')
        part_command = member.suite.part_command
        assert part_command is not None  # a suite of a part runs its tests by part
        command = part_command(root, part.root,
                               f'{reports}/{part_report(language, index, part)}', member.prefix)
        commands.append(f'# Part {part.name}\n({command}); record $?')
    lines.append('COPY run-tests.sh run-parts.sh /runner/')
    if not service:
        # The base is a part's runner: its own command is not the service's.
        lines.append('CMD []')
    run_parts = '\n'.join([
        '#!/usr/bin/env bash',
        '# Generated by the host: runs the tests of each part in turn, each writing its own',
        '# report, and exits as the worst of them did: a stopped run first, then any other',
        '# error, then failed tests.',
        'set -u',
        'status=0',
        'record() {',
        '    case "$1" in',
        '        0) ;;',
        '        124|125|137|143) status=$1 ;;',
        '        1) if [ "$status" -eq 0 ]; then status=1; fi ;;',
        '        *) case "$status" in 124|125|137|143) ;; *) status=$1 ;; esac ;;',
        '    esac',
        '}',
        f'mkdir -p {shlex.quote(reports)}',
        *(['# The service, with its own runner\'s command.',
           '(cd /tests && "$@"); record $?'] if service else []),
        *commands,
        'exit "$status"',
        ''])
    run_tests = '\n'.join([
        '#!/usr/bin/env bash',
        '# Trusted host-supplied runner: no generated commands or worker secrets. The tests',
        '# are in-process, so there is no service to wait for.',
        'set -eu',
        '# 124/137 indicate timeout/forced termination, not assertion failure.',
        f'exec timeout --signal=TERM --kill-after=10 {COMPOSITE_SECONDS} '
        'bash /runner/run-parts.sh "$@"',
        ''])
    return {'Dockerfile': '\n'.join(lines) + '\n', 'run-tests.sh': run_tests,
            'run-parts.sh': run_parts, '.dockerignore': '*\n!run-tests.sh\n!run-parts.sh\n'}


async def _remove_tags(tags: list[str]) -> None:
    for tag in tags:
        try:
            await docker_agents.docker('image', 'rm', tag, timeout=60)
        except Exception:
            activity.logger.warning('Could not remove the component runner image %s', tag)


async def build_runner(language: Language, session_dir: Path, tests: Path,
                       log_path: Path, repository: Path | None = None) -> str:
    """Build the suite's runner with the sources in `repository`, by default as the
    delivery started from them: the build collects the suite, so a test that does not
    even import fails here. A suite of several parts builds each part's runner first and
    then one image that runs them all."""
    parts, suite = await run_sync(delivery_suite, language, session_dir)
    if repository is None:
        repository = await run_sync(record_baseline, language, session_dir)
    work = tests.parent / 'runners'
    members = _members(suite, parts)
    if not isinstance(suite, CompositeSuite):
        await run_sync(work.mkdir)
        return await _build_member(parts[0], suite, session_dir, repository, tests, work,
                                   log_path)
    token = uuid.uuid4().hex
    images: dict[str, str] = {}
    tags: list[str] = []
    try:
        for index, (part, member) in enumerate(members):
            own = work / str(index)
            await run_sync(own.mkdir, parents=True)
            log = log_path.with_name(f'{log_path.stem}-{_slug(part.name)}{log_path.suffix}')
            try:
                image = await _build_member(part, member.suite, session_dir, repository,
                                            tests / member.directory, own, log)
            except RuntimeError as exc:
                raise RuntimeError(
                    f'The component tests of {"the service" if part.is_service else "part " + part.name} '
                    f'could not be built: {exc}') from exc
            tag = f'temporal-component-part:{token}-{index}'
            await docker_agents.docker('tag', image, tag, timeout=60)
            tags.append(tag)
            images[member.name] = tag
        context = work / 'composite' / 'context'
        await run_sync(context.mkdir, parents=True)
        for name, text in composite_files(language, members, images).items():
            await run_sync((context / name).write_text, text, encoding='utf-8')
        try:
            return await build_image(context, 'component test runner', log_path)
        except RuntimeError as exc:
            raise RuntimeError('The runner of the component tests of every part could not '
                               f'be built: {exc}') from exc
    finally:
        await _remove_tags(tags)


def stage(language: Language, parts: list[ComponentPart], suite: ComponentSuite,
          session_dir: Path, frozen: dict[str, str], requirement_ids: list[str] | None,
          repository: Path | None = None) -> StageSpec:
    """`frozen` holds the tests, by identity, whose source the author must leave as it is,
    and `requirement_ids` the criteria it may name as unobservable instead; None for a
    repair, which may not. Only the first run must add a test. `repository` is the code
    the runner is built with, by default as the delivery started from it."""
    return StageSpec(
        role='test_generation', artifact_subdir=ARTIFACT_DIR, workspace_name='tests',
        output_subdir='suite', heartbeat='Generating/collecting component tests',
        image_label='component test runner',
        image_build=dataclasses.replace(default_agent_image('test_generation', 'tests'),
                                        dockerfile=language.agent_dockerfile),
        prepare_inputs=partial(prepare_inputs, language, parts, suite),
        sanitize_output=partial(copy_test_suite, suite),
        validate_output=partial(validate_output, suite, frozen, requirement_ids is not None),
        declined_result=(partial(read_unobservable, requirement_ids)
                         if requirement_ids is not None else None),
        build_output=partial(build_runner, language, session_dir, repository=repository),
        # Kept as the suite is: its parts go to their own directories when it is pushed.
        work=lambda context: (WorkTarget('component-tests', lambda relative: True,
                                         prefix=COMPONENT_TESTS_DIR)
                              if context.get('repository') else None),
    )


def frozen_tests(suite: ComponentSuite, repository: dict[str, str], published: Path,
                 repair: dict[str, Any] | None) -> dict[str, str]:
    """The tests, by identity, an author of the component tests must leave as they are.
    The first run adds to the repository's tests. A repair of tests a diagnosis found
    broken may change those alone, and a correction of review findings concerns the
    delivery's own tests: the repository's that it left as they were stay so."""
    if not repair:
        return repository
    cases = suite.test_cases(published)
    if repair.get('review'):
        return {case: source for case, source in repository.items()
                if cases.get(case) == source}
    broken = {suite.case_id(item['class_name'], item['name'])
              for item in repair.get('broken_tests', [])}
    return {case: source for case, source in cases.items() if case not in broken}


@activity.defn
async def generate_component_tests(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    """Regression tests of a change's criteria routed to a seam, added to the repository's
    component tests of each part with routes. Like the contract tests' first author, the
    author may instead name criteria it finds no test of the seam can observe
    (`unobservable`). With `repair` in the context it corrects the suite it published:
    the tests a diagnosis found broken, or the code review's findings about it."""
    language = session_language(context.get('language'))
    if not context.get('brief'):
        raise ApplicationError("Component tests are written only for a change's criteria",
                               non_retryable=True)
    session_dir = GENERATED_CODE_DIR / session_id
    parts, suite = await run_sync(delivery_suite, language, session_dir)
    names = {part.name for part in parts}
    try:
        routes = component_routes(context.get('routes'),
                                  tuple(part.name for part in parts if not part.is_service))
    except RuntimeError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    if not routes:
        raise ApplicationError('No criterion is routed to the component tests',
                               non_retryable=True)
    unchecked = sorted({route['seam'].get('part', SERVICE_PART) for route in routes.values()}
                       - names)
    if unchecked:
        raise ApplicationError('Criteria are routed to seams of parts whose component contract '
                               f'the routing did not check: {", ".join(unchecked)}',
                               non_retryable=True)
    context = {**context, 'routes': routes}
    snapshot = session_dir / SNAPSHOT_DIR / COMPONENT_SNAPSHOT
    continued = bool(context.get('repository'))
    repository = await run_sync(suite.test_cases, snapshot) if continued else {}
    repair = context.get('repair')
    if repair:
        # The suite a repair continues, after this worker lost it.
        await restore_stage_output(stage(language, parts, suite, session_dir, {}, None),
                                   session_dir, context)
    frozen = await run_sync(frozen_tests, suite, repository,
                            session_dir / ARTIFACT_DIR / 'suite', repair)
    # A change that added its seam first is run against the code with the seam.
    prepared = session_dir / SEAM_DIR / 'service' if context.get('seam_prepared') else None
    if prepared and not await run_sync(prepared.is_dir):
        raise ApplicationError('The code with the seam the change added is unavailable',
                               non_retryable=True)
    result = await run_stage(stage(language, parts, suite, session_dir, frozen,
                                   None if repair else list(routes), prepared),
                             session_id, context, GENERATED_CODE_DIR)
    if 'questions' in result or 'unobservable' in result or 'provider_limit' in result:
        return result
    cases = await run_sync(suite.test_cases, Path(result['path']))
    return {**result,
            'added_tests': sorted(cases.keys() - repository.keys()),
            'changed_tests': sorted(case for case, source in repository.items()
                                    if case in cases and cases[case] != source),
            'removed_tests': sorted(repository.keys() - cases.keys())}
