"""Host-only code review of a verified delivery's own changes."""
import dataclasses
import difflib
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
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
from app.github.checkout import CHECKOUT_DIR, SNAPSHOT_DIR
from app.implementation.activities import copy_service
from app.languages import Language
from app.requirements.document import without_repeated_tables
from app.review.agent import FINDINGS
from app.review.models import (
    INSTRUCTIONS_CHARS, MAX_FINDINGS, PATH_CHARS, REASON_CHARS, REVIEW_TARGETS, SUMMARY_CHARS,
    ReviewFinding,
)
from app.temporal.tracing import activity_metadata
from app.tracing import trace_operation

logger = logging.getLogger(__name__)

# The service as the delivery found it, recorded once before the first review.
BASELINE_DIR = 'review-baseline'
MAX_FINDINGS_BYTES = 256 * 1024
# The diff the reviewer reads; larger changes are cut and the reviewer reads the files.
MAX_DIFF_CHARS = 2 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
LISTED_CHANGES = 500


def record_baseline(language: Language, session_dir: Path) -> Path:
    """The service sources the delivery started from: the repository's, or none. Taken
    once, so a later review compares with the same sources whatever was pushed since."""
    baseline = session_dir / BASELINE_DIR
    target = baseline / 'service'
    if not target.is_dir():
        baseline.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=baseline) as temporary:
            clean = Path(temporary) / 'service'
            checkout = session_dir / CHECKOUT_DIR
            if checkout.is_dir():
                copy_service(language, checkout, clean)
            else:
                clean.mkdir()
            os.replace(clean, target)
    return target


def _files(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    return {item.relative_to(root).as_posix(): item for item in sorted(root.rglob('*'))
            if item.is_file() and not item.is_symlink()}


def _lines(path: Path | None) -> list[str] | None:
    """The file's lines, or None for a binary or oversized file."""
    if path is None:
        return []
    if path.stat().st_size > MAX_FILE_BYTES:
        return None
    try:
        return path.read_text(encoding='utf-8').splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def changed_files(before: Path, after: Path, prefix: str) -> tuple[list[str], str]:
    """The paths, under `prefix`, that differ between the trees, and their unified diff."""
    old, new = _files(before), _files(after)
    changed: list[str] = []
    parts: list[str] = []
    for relative in sorted(old.keys() | new.keys()):
        source, target = old.get(relative), new.get(relative)
        if source and target and source.read_bytes() == target.read_bytes():
            continue
        name = f'{prefix}/{relative}'
        changed.append(name)
        a, b = _lines(source), _lines(target)
        if a is None or b is None:
            parts.append(f'Binary or large file {name} differs\n')
            continue
        parts.extend(difflib.unified_diff(
            a, b, fromfile=f'a/{name}' if source else '/dev/null',
            tofile=f'b/{name}' if target else '/dev/null'))
        if parts and not parts[-1].endswith('\n'):
            parts[-1] += '\n'
    return changed, ''.join(parts)


def prepare_review(language: Language, session_dir: Path, context: dict[str, Any],
                   workspace: Path) -> dict[str, list[str]]:
    """Both verified source trees, their changes and the contracts; nothing else. Returns
    the changed files of each side."""
    service = session_dir / 'implementation/service'
    suite = session_dir / 'tests/suite'
    if not all(item.is_dir() for item in (service, suite)):
        raise ApplicationError('The verified sources are unavailable', non_retryable=True)
    service_before = record_baseline(language, session_dir)
    snapshot = session_dir / SNAPSHOT_DIR / 'suite'
    tests_before = snapshot if snapshot.is_dir() else language.test_suite.template
    review = workspace / 'review'
    copy_service(language, service, review / 'service')
    copy_regular_files(suite, review / 'tests', include=lambda relative: True)
    service_changes, service_diff = changed_files(service_before, review / 'service', 'service')
    tests_changes, tests_diff = changed_files(tests_before, review / 'tests', 'tests')
    diff = service_diff + tests_diff
    if len(diff) > MAX_DIFF_CHARS:
        diff = (diff[:MAX_DIFF_CHARS]
                + '\n[The diff is cut here; read the remaining changed files directly.]\n')
    (review / 'changes.diff').write_text(diff, encoding='utf-8')
    changes = {'service': service_changes, 'tests': tests_changes}
    contracts = without_repeated_tables(
        {key: value for key, value in context.items()
         if key not in ('instructions', 'repository', 'language')})
    index, files = on_demand_contracts(contracts, len(json.dumps(contracts))) or (contracts, {})
    write_language(workspace, language, label=language.label)
    if files:
        write_contracts(workspace, files)
    instructions = str(context.get('instructions') or '')[:INSTRUCTIONS_CHARS]
    (workspace / 'input.json').write_text(json.dumps(
        {**index,
         'changes': {side: paths[:LISTED_CHANGES] for side, paths in changes.items()},
         **({'instructions': instructions} if instructions else {})},
        ensure_ascii=False))
    return changes


def read_findings(output: Path, changed: set[str]) -> list[ReviewFinding]:
    """The agent's output is untrusted: accept bounded, well-formed findings about the
    delivery's own changes only."""
    file = output / FINDINGS
    if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_FINDINGS_BYTES:
        raise RuntimeError('The code review did not produce its findings')
    try:
        result = json.loads(file.read_text())
    except ValueError as exc:
        raise RuntimeError('The code review findings are not valid JSON') from exc
    if not isinstance(result, dict) or not isinstance(result.get('findings'), list):
        raise RuntimeError('The code review findings are not a list')
    findings: list[ReviewFinding] = []
    for item in result['findings']:
        if not isinstance(item, dict) or item.get('target') not in REVIEW_TARGETS:
            continue
        summary = str(item.get('summary') or '').strip()
        if not summary:
            continue
        finding: ReviewFinding = {
            'summary': summary[:SUMMARY_CHARS], 'target': item['target'],
            'reason': str(item.get('reason') or '').strip()[:REASON_CHARS]}
        path = str(item.get('path') or '').strip().removeprefix('./')
        if path:
            if path not in changed:
                # Code the delivery did not change is not the review's matter.
                logger.info('Dropped a review finding about unchanged %s', path[:PATH_CHARS])
                continue
            finding['path'] = path[:PATH_CHARS]
        findings.append(finding)
        if len(findings) == MAX_FINDINGS:
            break
    return findings


async def run_review(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    """The findings, or `questions` the agent asked: then it is paused, and the same call
    with ANSWERS_KEY in the context continues it."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    answers = context.get(ANSWERS_KEY)
    context = {key: value for key, value in context.items() if key != ANSWERS_KEY}
    language = session_language(context.get('language'))
    session_dir = GENERATED_CODE_DIR / session_id
    artifact_dir = session_dir / 'review'
    paused = artifact_dir / docker_agents.PAUSED_DIR
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        workspace = work / 'workspace'
        if answers:
            # The paused review continues in the workspace it left.
            docker_agents.resume_workspace(paused, workspace, answers)
            changes = json.loads((workspace / 'input.json').read_text()).get('changes') or {}
        else:
            workspace.mkdir()
            changes = prepare_review(language, session_dir, context, workspace)
        changed = {path for paths in changes.values() for path in paths}
        if not changed:
            shutil.rmtree(paused, ignore_errors=True)
            logger.info('Nothing to review (session=%s)', session_id)
            return {'session_id': session_id, 'findings': [], 'path': str(artifact_dir)}
        async with docker_agents.keep_activity_alive('Reviewing the delivery'):
            output = work / 'output'
            try:
                await docker_agents.run_agent(
                    'review', session_id, workspace, output, 'review',
                    artifact_dir / 'agent.log', paused=paused,
                    image_build=dataclasses.replace(
                        docker_agents.default_agent_image('review', 'review'),
                        dockerfile=language.agent_dockerfile))
            except docker_agents.AgentPaused as exc:
                return paused_result(session_id, exc)
        findings = read_findings(output, changed)
    result = {'session_id': session_id, 'findings': findings, 'path': str(artifact_dir)}
    manifest = artifact_dir / f'{uuid.uuid4().hex}.json'
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(manifest, artifact_dir / FINDINGS)
    logger.info('Code review finished (session=%s, findings=%d)', session_id, len(findings))
    return result


@activity.defn
async def review_changes(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    with trace_operation('review-changes', session_id=session_id, feature='review',
                         input=context.get('instructions'),
                         metadata=activity_metadata()) as span:
        result = await run_review(session_id, context)
        if span is not None:
            span.update(output=result)
        return result
