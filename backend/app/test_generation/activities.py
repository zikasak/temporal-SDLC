"""Contracts-only test authoring policies and trusted standalone runner publication."""
import dataclasses
import json
import shutil
from functools import partial
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent_workflows.contracts import on_demand_contracts, write_contracts
from app.agent_workflows.host import (
    StageInputs, StageSpec, copy_regular_files, run_stage, session_language, write_language,
)
from app.api_design.openapi import render_openapi
from app.config import GENERATED_CODE_DIR
from app.docker_agents import default_agent_image
from app.github.checkout import CHECKOUT_DIR, TESTS_DIR
from app.languages import Language, TestSuite
from app.requirements.document import without_repeated_tables

# Supplied by every language's template; the runner never comes from the test author.
RUNNER_FILES = ('Dockerfile', '.dockerignore', 'run-tests.sh')
MAX_NAMED_TESTS = 5


def copy_test_suite(suite: TestSuite, source: Path, target: Path) -> None:
    """Publish test sources/configuration, excluding links and untrusted runner files."""
    published = {Path(item) for item in suite.published}
    copy_regular_files(source, target, include=lambda relative: (
        relative in published or not published.isdisjoint(relative.parents)
    ))
    # The runner always compiles tests, even if the agent changes its Dockerfile.
    for name in RUNNER_FILES:
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
        # A change to an existing service adds tests for its brief to the existing suite.
        **({'brief': context['brief']} if context.get('brief') else {}),
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
        shutil.copytree(suite.template, workspace / 'tests')
        if repair:
            # A repair continues from the published suite; runner files stay the template's.
            copy_test_suite(suite, previous, workspace / 'tests')
        elif repository and (checkout / TESTS_DIR).is_dir():
            copy_test_suite(suite, checkout / TESTS_DIR, workspace / 'tests')
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
    if frozen is None:
        return
    cases = suite.test_cases(clean)
    changed = sorted(case for case, source in frozen.items() if cases.get(case) != source)
    if changed:
        raise RuntimeError('Test generation changed or removed tests it may not touch: '
                           + ', '.join(changed[:MAX_NAMED_TESTS]))
    if must_add and not cases.keys() - frozen.keys():
        raise RuntimeError('Test generation did not add a regression test')


def stage(language: Language, frozen: dict[str, str] | None = None,
          must_add: bool = False) -> StageSpec:
    """`frozen` holds the tests, by identity, whose source the author must leave as it is."""
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
    )


@activity.defn
async def generate_service_tests(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    language = session_language(context.get('language'))
    suite = language.test_suite
    session_dir = GENERATED_CODE_DIR / session_id
    repair = context.get('repair')
    change = bool(context.get('brief'))
    # A change to an existing service adds to the repository's tests and leaves them alone.
    repository = suite.test_cases(session_dir / CHECKOUT_DIR / TESTS_DIR) if change else {}
    frozen = None
    if repair:
        # Only the tests the diagnosis found broken may differ from the published suite.
        broken = {suite.case_id(item['class_name'], item['name'])
                  for item in repair.get('broken_tests', [])}
        frozen = {case: source for case, source in suite.test_cases(
            session_dir / 'tests/suite').items() if case not in broken}
    elif change:
        frozen = repository
    result = await run_stage(stage(language, frozen, must_add=change and not repair),
                             session_id, context, GENERATED_CODE_DIR)
    if not change:
        return result
    # What the suite now differs in from the repository's, for the baseline run.
    cases = suite.test_cases(Path(result['path']))
    return {**result,
            'added_tests': sorted(cases.keys() - repository.keys()),
            'changed_tests': sorted(case for case, source in repository.items()
                                    if case in cases and cases[case] != source),
            'removed_tests': sorted(repository.keys() - cases.keys())}
