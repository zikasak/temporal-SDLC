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
from app.agent_workflows.contracts import on_demand_contracts, write_contracts
from app.agent_workflows.host import (
    ANSWERS_KEY, copy_regular_files, paused_result, session_language, write_language,
)
from app.config import GENERATED_CODE_DIR
from app.diagnosis.agent import VERDICT
from app.diagnosis.models import (
    INSTRUCTION_CHARS, SUMMARY_CHARS, TARGETS, Diagnosis, failed_tests,
)
from app.implementation.activities import copy_service
from app.languages import Language
from app.requirements.document import without_repeated_tables
from app.verification.models import TestRef
from app.temporal.tracing import activity_metadata
from app.tracing import trace_operation

logger = logging.getLogger(__name__)

MAX_VERDICT_BYTES = 256 * 1024
MAX_BROKEN_TESTS = 200


def prepare_review(language: Language, session_dir: Path, context: dict[str, Any],
                   workspace: Path) -> None:
    """Both published source trees, the failed run and the contracts; nothing else."""
    verification = context.get('verification') or {}
    service = session_dir / 'implementation/service'
    suite = session_dir / 'tests/suite'
    # The run directory is derived here, never taken as a path from the payload.
    token = Path(str(verification.get('path', ''))).name
    run_dir = session_dir / 'verification' / token
    if (verification.get('status') != 'failed' or not re.fullmatch(r'[0-9a-f]{32}', token)
            or not all(item.is_dir() for item in (service, suite, run_dir))):
        raise ApplicationError('Failed verification artifacts are unavailable', non_retryable=True)
    contracts = without_repeated_tables(
        {key: value for key, value in context.items() if key != 'verification'})
    index, files = on_demand_contracts(contracts, len(json.dumps(contracts))) or (contracts, {})
    review = workspace / 'review'
    copy_service(language, service, review / 'service')
    copy_regular_files(suite, review / 'tests', include=lambda relative: True)
    # result.json only repeats the logs and the failures listed in the input.
    copy_regular_files(run_dir, review / 'verification',
                       include=lambda relative: relative.name != 'result.json')
    write_language(workspace, language, label=language.label,
                   report_pattern=language.test_reports.pattern)
    if files:
        write_contracts(workspace, files)
    counts = {key: verification.get(key, 0)
              for key in ('tests', 'passed', 'failures', 'errors', 'skipped')}
    (workspace / 'input.json').write_text(json.dumps(
        {**index, 'verification': {**counts, **failed_tests(verification)}}, ensure_ascii=False))


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


def read_verdict(output: Path, verification: dict[str, Any],
                 case_id: Callable[[str, str], str]) -> dict[str, Any]:
    """The agent's output is untrusted: accept one bounded, well-formed decision."""
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
    tests = (broken_tests(verdict.get('tests'), verification, case_id)
             if verdict['target'] == 'tests' else [])
    return {'target': verdict['target'], 'tests': tests, 'summary': summary[:SUMMARY_CHARS],
            'instructions': instructions[:INSTRUCTION_CHARS]}


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
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        workspace = work / 'workspace'
        artifact_dir.mkdir(exist_ok=True)
        if answers:
            # The paused review continues in the workspace it left.
            docker_agents.resume_workspace(paused, workspace, answers)
        else:
            workspace.mkdir()
            prepare_review(language, session_dir, context, workspace)
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
        verdict = read_verdict(output, context.get('verification') or {},
                               language.test_suite.case_id)
        result: Diagnosis = {'session_id': session_id, **verdict,
                             'path': str(artifact_dir)}
    manifest = artifact_dir / f'{uuid.uuid4().hex}.json'
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(manifest, artifact_dir / VERDICT)
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
