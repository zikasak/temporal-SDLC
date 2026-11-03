"""Host-only review of a failed verification: which side must be repaired."""
import dataclasses
import json
import logging
import os
import re
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app import docker_agents
from app.asyncio_utils import async_context, run_sync
from app.agent_workflows.contracts import on_demand_contracts, write_contracts
from app.agent_workflows.host import (
    ANSWERS_KEY, copy_regular_files, paused_result, session_language, write_language,
)
from app.config import GENERATED_CODE_DIR
from app.diagnosis.agent import VERDICT
from app.diagnosis.models import (
    INSTRUCTION_CHARS, SUITES, SUMMARY_CHARS, TARGETS, Diagnosis, RepairSuite, failed_tests,
)
from app.github.checkout import SNAPSHOT_DIR
from app.implementation.activities import copy_service
from app.languages import Language
from app.requirements.document import without_repeated_tables
from app.routing.components import ROUTED_CONTRACT
from app.verification.models import (
    COMPONENT_RUNS_DIR, COMPONENT_TESTS_ARTIFACT, VERIFICATION_RUNS_DIR, TestRef,
)
from app.temporal.tracing import activity_metadata
from app.tracing import trace_operation

logger = logging.getLogger(__name__)

MAX_VERDICT_BYTES = 256 * 1024
MAX_BROKEN_TESTS = 200


# Where the review workspace holds a change's component tests, the failed run of them and
# the stubs of the component contract they were written from.
COMPONENT_TESTS = 'component-tests'
COMPONENT_VERIFICATION = 'component-verification'
COMPONENT_CONTRACT = 'component-contract'
# The keys of the activity context that describe the runs, not the task.
RUN_KEYS = ('verification', 'component_verification', 'contract_suite')


def _run_dir(session_dir: Path, artifact: str, run: dict[str, Any]) -> Path:
    """The artifacts of a failed run, derived here, never taken as a path from the payload."""
    token = Path(str(run.get('path', ''))).name
    run_dir = session_dir / artifact / token
    if not re.fullmatch(r'[0-9a-f]{32}', token) or not run_dir.is_dir():
        raise ApplicationError('Failed verification artifacts are unavailable', non_retryable=True)
    return run_dir


def _summary(run: dict[str, Any], status: bool) -> dict[str, Any]:
    """What the agent is told of a run; with its `status` when there are two runs."""
    counts = {key: run.get(key, 0) for key in ('tests', 'passed', 'failures', 'errors', 'skipped')}
    return {**({'status': run.get('status')} if status else {}), **counts, **failed_tests(run)}


def prepare_review(language: Language, session_dir: Path, context: dict[str, Any],
                   workspace: Path) -> None:
    """Both published source trees, the failed run and the contracts; nothing else. A
    change with component tests (`component_verification` in the context) also gets
    those tests, the stubs they were written from and their run; then either run may be
    the failed one, and the contract tests it ran (`contract_suite`) may be the
    repository's own, or none at all."""
    verification = context.get('verification')
    component = context.get('component_verification')
    suite_dir = context.get('contract_suite') or 'tests'
    service = session_dir / 'implementation/service'
    if suite_dir not in ('tests', SNAPSHOT_DIR):
        raise ApplicationError('Invalid test suite', non_retryable=True)
    suite = session_dir / suite_dir / 'suite'
    failed = [run for run in (verification, component) if run and run.get('status') == 'failed']
    if not failed or not service.is_dir() or (verification and not suite.is_dir()):
        raise ApplicationError('Failed verification artifacts are unavailable', non_retryable=True)
    contracts = without_repeated_tables(
        {key: value for key, value in context.items() if key not in RUN_KEYS})
    index, files = on_demand_contracts(contracts, len(json.dumps(contracts))) or (contracts, {})
    review = workspace / 'review'
    copy_service(language, service, review / 'service')
    runs: dict[str, Any] = {}
    if verification:
        copy_regular_files(suite, review / 'tests', include=lambda relative: True)
        runs['verification'] = _summary(verification, component is not None)
        if verification.get('status') == 'failed':
            # result.json only repeats the logs and the failures listed in the input.
            copy_regular_files(_run_dir(session_dir, VERIFICATION_RUNS_DIR, verification),
                               review / 'verification',
                               include=lambda relative: relative.name != 'result.json')
    if component is not None:
        tests, stubs = session_dir / COMPONENT_TESTS_ARTIFACT / 'suite', session_dir / ROUTED_CONTRACT
        if not tests.is_dir() or not stubs.is_dir():
            raise ApplicationError('The component tests are unavailable', non_retryable=True)
        copy_regular_files(tests, review / COMPONENT_TESTS, include=lambda relative: True)
        copy_regular_files(stubs, review / COMPONENT_CONTRACT, include=lambda relative: True)
        runs['component_verification'] = _summary(component, True)
        if component.get('status') == 'failed':
            copy_regular_files(_run_dir(session_dir, COMPONENT_RUNS_DIR, component),
                               review / COMPONENT_VERIFICATION,
                               include=lambda relative: relative.name != 'result.json')
        if verification and suite_dir == SNAPSHOT_DIR:
            # Its tests passed without the change, and no author of the delivery wrote any.
            runs['contract_suite'] = 'repository'
    write_language(workspace, language, label=language.label,
                   report_pattern=language.test_reports.pattern)
    if files:
        write_contracts(workspace, files)
    (workspace / 'input.json').write_text(json.dumps({**index, **runs}, ensure_ascii=False))


def broken_tests(named: Any, verification: dict[str, Any],
                 case_id: Callable[[str, str], str]) -> list[TestRef]:
    """The tests a verdict against the tests may have repaired: those it names that did
    fail in the run. A test that did not fail cannot be found broken."""
    failed = {case_id(item['class_name'], item['name']): item
              for item in verification.get('test_failures') or []}
    cases = {case_id(item['class_name'], item['name'])
             for item in (named if isinstance(named, list) else [])[:MAX_BROKEN_TESTS]
             if isinstance(item, dict) and all(isinstance(item.get(key), str)
                                               for key in ('class_name', 'name'))}
    tests = [{'class_name': failed[case]['class_name'], 'name': failed[case]['name']}
             for case in sorted(cases & failed.keys())]
    if not tests:
        raise RuntimeError('Diagnosis verdict against the tests names no failing test as broken')
    return tests


def _suite(verdict: dict[str, Any], failed: dict[str, dict[str, Any]],
           repository_contract: bool) -> RepairSuite:
    """The suite a verdict against the tests of a change with component tests condemns:
    the one it names, or the only one that failed."""
    suite = verdict.get('suite')
    if suite is None and len(failed) == 1:
        suite = next(iter(failed))
    if suite not in SUITES:
        raise RuntimeError('Diagnosis verdict against the tests does not name the contract or '
                           'the component tests')
    if suite not in failed:
        raise RuntimeError(f'Diagnosis verdict condemns the {suite} tests, which did not fail')
    if suite == 'contract' and repository_contract:
        raise RuntimeError("Diagnosis verdict condemns the repository's own contract tests, "
                           'which passed without the change and which no author of this '
                           'delivery may change')
    return suite


def read_verdict(output: Path, verification: dict[str, Any] | None,
                 case_id: Callable[[str, str], str],
                 component: dict[str, Any] | None = None,
                 component_case_id: Callable[[str, str], str] | None = None,
                 repository_contract: bool = False) -> dict[str, Any]:
    """The agent's output is untrusted: accept one bounded, well-formed decision. A
    verdict against the tests of a change with component tests (`component`) names the
    suite they belong to, and its broken tests are tests of that suite that failed."""
    file = output / VERDICT
    if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_VERDICT_BYTES:
        raise RuntimeError('Diagnosis did not produce a verdict')
    try:
        verdict = json.loads(file.read_text())
    except ValueError as exc:
        raise RuntimeError('Diagnosis verdict is not valid JSON') from exc
    if not isinstance(verdict, dict) or verdict.get('target') not in TARGETS:
        raise RuntimeError('Diagnosis verdict does not name the implementation or the tests')
    summary, instructions = (str(verdict.get(key) or '').strip()
                             for key in ('summary', 'instructions'))
    if not instructions:
        raise RuntimeError('Diagnosis verdict has no repair instructions')
    result = {'target': verdict['target'], 'tests': [], 'summary': summary[:SUMMARY_CHARS],
              'instructions': instructions[:INSTRUCTION_CHARS]}
    if verdict['target'] != 'tests':
        return result
    if component is None:
        return {**result, 'tests': broken_tests(verdict.get('tests'), verification or {},
                                                case_id)}
    runs = {'contract': (verification, case_id),
            'component': (component, component_case_id or case_id)}
    suite = _suite(verdict, {name: run for name, (run, _) in runs.items()
                             if run and run.get('status') == 'failed'}, repository_contract)
    run, identity = runs[suite]
    return {**result, 'tests': broken_tests(verdict.get('tests'), run, identity),
            'suite': suite}


async def run_diagnosis(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    """The diagnosis, or `questions` the agent asked: then it is paused, and the same call
    with ANSWERS_KEY in the context continues it."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    answers = context.get(ANSWERS_KEY)
    context = {key: value for key, value in context.items() if key != ANSWERS_KEY}
    language = session_language(context.get('language'))
    session_dir = GENERATED_CODE_DIR / session_id
    artifact_dir = session_dir / 'diagnosis'
    paused = artifact_dir / docker_agents.PAUSED_DIR
    async with async_context(tempfile.TemporaryDirectory) as temporary:
        work = Path(temporary)
        workspace = work / 'workspace'
        await run_sync(artifact_dir.mkdir, exist_ok=True)
        if answers:
            # The paused review continues in the workspace it left.
            await run_sync(docker_agents.resume_workspace, paused, workspace, answers)
        else:
            await run_sync(workspace.mkdir)
            await run_sync(prepare_review, language, session_dir, context, workspace)
        async with docker_agents.keep_activity_alive('Diagnosing failed verification'):
            output = work / 'output'
            try:
                await docker_agents.run_agent(
                    'diagnosis', session_id, workspace, output, 'review',
                    artifact_dir / 'agent.log', paused=paused,
                    image_build=dataclasses.replace(
                        docker_agents.default_agent_image('diagnosis', 'review'),
                        dockerfile=language.agent_dockerfile))
            except docker_agents.AgentPaused as exc:
                return paused_result(session_id, exc)
        component = context.get('component_verification')
        verdict = await run_sync(
            read_verdict, output, context.get('verification'), language.test_suite.case_id,
            component, language.component_suite.case_id if language.component_suite else None,
            context.get('contract_suite') == SNAPSHOT_DIR)
        result: Diagnosis = {'session_id': session_id, **verdict,
                             'path': str(artifact_dir)}
    manifest = artifact_dir / f'{uuid.uuid4().hex}.json'
    await run_sync(
        manifest.write_text, json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    await run_sync(os.replace, manifest, artifact_dir / VERDICT)
    logger.info('Diagnosis finished (session=%s, target=%s)', session_id, result['target'])
    return result


@activity.defn
async def diagnose_failure(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    with trace_operation('diagnose-failure', session_id=session_id, feature='diagnosis',
                         input=context.get('verification'), metadata=activity_metadata()) as span:
        result = await run_diagnosis(session_id, context)
        if span is not None:
            span.update(output=result)
        return result
