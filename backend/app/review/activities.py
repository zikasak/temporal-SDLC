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
from app.asyncio_utils import async_context, run_sync
from app.agent_workflows.contracts import on_demand_contracts, write_contracts
from app.agent_workflows.host import (
    ANSWERS_KEY, copy_regular_files, paused_result, session_language, write_language,
)
from app.config import GENERATED_CODE_DIR
from app.github.checkout import (
    CHECKOUT_DIR, COMPONENT_SNAPSHOT, COMPONENT_TESTS_DIR, SNAPSHOT_DIR,
)
from app.implementation.activities import checkout_parts, copy_service
from app.languages import Language
from app.requirements.document import without_repeated_tables
from app.routing.parts import component_suite, read_routed_parts
from app.test_generation.activities import copy_test_suite
from app.review.agent import FINDINGS
from app.review.models import (
    MAX_FINDINGS, PATH_CHARS, PREVIOUS_SUMMARY_CHARS, REASON_CHARS, REVIEW_TARGETS,
    SUMMARY_CHARS, REVIEW_INSTRUCTIONS_CHARS, ReviewFinding,
)
from app.verification.models import COMPONENT_TESTS_ARTIFACT
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
# Larger changes are split by file into parts that separate review runs take in turn, so
# each part's diff and the code around it fit one agent's context.
PART_DIFF_CHARS = 60_000
# Which part of the changes a review run takes, counted from 0, in the activity context.
PART_KEY = 'review_part'
# The findings earlier rounds of the same run had corrected, in the activity context.
PREVIOUS_KEY = 'previous_findings'


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


def changed_files(before: Path, after: Path, prefix: str) -> dict[str, str]:
    """The paths, under `prefix`, that differ between the trees, each with its unified diff,
    in path order."""
    old, new = _files(before), _files(after)
    changed: dict[str, str] = {}
    for relative in sorted(old.keys() | new.keys()):
        source, target = old.get(relative), new.get(relative)
        if source and target and source.read_bytes() == target.read_bytes():
            continue
        name = f'{prefix}/{relative}'
        a, b = _lines(source), _lines(target)
        if a is None or b is None:
            changed[name] = f'Binary or large file {name} differs\n'
            continue
        lines = list(difflib.unified_diff(
            a, b, fromfile=f'a/{name}' if source else '/dev/null',
            tofile=f'b/{name}' if target else '/dev/null'))
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        changed[name] = ''.join(lines)
    return changed


def review_parts(diffs: dict[str, str]) -> list[list[str]]:
    """Consecutive changed files grouped into parts of at most PART_DIFF_CHARS of diff,
    so files of one directory tend to share a part; a larger file is a part of its own."""
    parts: list[list[str]] = []
    size = 0
    for path, diff in diffs.items():
        if not parts or size + len(diff) > PART_DIFF_CHARS:
            parts.append([])
            size = 0
        parts[-1].append(path)
        size += len(diff)
    return parts


def prepare_review(language: Language, session_dir: Path, context: dict[str, Any],
                   workspace: Path, part: int = 0) -> tuple[dict[str, list[str]], int]:
    """Both verified source trees, the changes of the given part and the contracts; nothing
    else. Returns the changed files of each side in that part, and the number of parts.
    A change with criteria routed to seams (`component_routes` in the context) also has
    its component tests reviewed, and the stubs of its component contract, which no
    author of the delivery may change; it has contract tests of its own only when
    `contract_tests` says so."""
    component = bool(context.get('component_routes'))
    contract = not component or bool(context.get('contract_tests'))
    service = session_dir / 'implementation/service'
    suite = session_dir / 'tests/suite'
    trees = [service, *([suite] if contract else [])]
    if component:
        try:
            routed = read_routed_parts(language, session_dir)
            component_tests = component_suite(language, routed)
        except ValueError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        tests = session_dir / COMPONENT_TESTS_ARTIFACT / 'suite'
        # The stubs the routing checked of each part, and where the repository has them.
        stubs = [(item.routed_contract(session_dir), item.published_dir()) for item in routed]
        trees += [tests, *(kept for kept, _ in stubs)]
    if not all(item.is_dir() for item in trees):
        raise ApplicationError('The verified sources are unavailable', non_retryable=True)
    service_before = record_baseline(language, session_dir)
    review = workspace / 'review'
    copy_service(language, service, review / 'service')
    diffs = changed_files(service_before, review / 'service', 'service')
    sides = ['service']
    if contract:
        snapshot = session_dir / SNAPSHOT_DIR / 'suite'
        tests_before = snapshot if snapshot.is_dir() else language.test_suite.template
        copy_regular_files(suite, review / 'tests', include=lambda relative: True)
        diffs.update(changed_files(tests_before, review / 'tests', 'tests'))
        sides.append('tests')
    if component:
        everything = lambda relative: True  # noqa: E731
        copy_regular_files(tests, review / COMPONENT_TESTS_DIR, include=everything)
        with tempfile.TemporaryDirectory() as temporary:
            # Recorded before any author ran, as the template's files when the repository
            # had no component tests: of the parts the suite has a project for alone.
            before = Path(temporary) / 'before'
            recorded = session_dir / SNAPSHOT_DIR / COMPONENT_SNAPSHOT
            if recorded.is_dir():
                copy_test_suite(component_tests, recorded, before)
            diffs.update(changed_files(before, review / COMPONENT_TESTS_DIR,
                                       COMPONENT_TESTS_DIR))
        sides.append(COMPONENT_TESTS_DIR)
        for kept, published in stubs:
            copy_regular_files(kept, review / published, include=everything)
            # The stubs the repository published, which the routing reused or extended.
            diffs.update(changed_files(session_dir / CHECKOUT_DIR / published,
                                       review / published, published.as_posix()))
            sides.append(published.as_posix())
    parts = review_parts(diffs)
    if part >= max(len(parts), 1):
        raise ApplicationError(f'The changes have no review part {part}', non_retryable=True)
    own = parts[part] if parts else []
    diff = ''.join(diffs[path] for path in own)
    if len(diff) > MAX_DIFF_CHARS:
        diff = (diff[:MAX_DIFF_CHARS]
                + '\n[The diff is cut here; read the remaining changed files directly.]\n')
    (review / 'changes.diff').write_text(diff, encoding='utf-8')

    def by_side(paths: list[str]) -> dict[str, list[str]]:
        return {side: [path for path in paths if path.startswith(f'{side}/')][:LISTED_CHANGES]
                for side in sides}

    changes = by_side(own)
    others = [path for path in diffs if path not in own]
    contracts = without_repeated_tables(
        {key: value for key, value in context.items()
         if key not in ('instructions', 'repository', 'language', 'contract_tests',
                        PREVIOUS_KEY)})
    index, files = on_demand_contracts(contracts, len(json.dumps(contracts))) or (contracts, {})
    write_language(workspace, language, label=language.label)
    if files:
        write_contracts(workspace, files)
    instructions = str(context.get('instructions') or '')[:REVIEW_INSTRUCTIONS_CHARS]
    # An onboarding changes no part of the repository but the service's Dockerfile.
    checkout = session_dir / CHECKOUT_DIR
    projects = (checkout_parts(language, checkout)
                if checkout.is_dir() and not context.get('onboarding') else [])
    (workspace / 'input.json').write_text(json.dumps(
        {**index, 'changes': changes,
         **({'contract_tests': contract} if component else {}),
         **({'repository_parts': projects} if projects else {}),
         **({PART_KEY: {'part': part + 1, 'of': len(parts), 'other_changes': by_side(others)}}
            if len(parts) > 1 else {}),
         **({'instructions': instructions} if instructions else {}),
         **({PREVIOUS_KEY: context[PREVIOUS_KEY]} if context.get(PREVIOUS_KEY) else {})},
        ensure_ascii=False))
    return changes, max(len(parts), 1)


def _same(summary: str) -> str:
    return ' '.join(summary.lower().split())


def _repeated(item: dict[str, Any], finding: ReviewFinding,
              previous: list[dict[str, Any]]) -> int | None:
    """The number of the earlier finding this one reports again: the one the reviewer
    names, or one with the very same target, file and summary."""
    numbers = {entry.get('number') for entry in previous}
    named = item.get('repeats')
    if isinstance(named, int) and not isinstance(named, bool) and named in numbers:
        return named
    for entry in previous:
        if (entry.get('target') == finding['target']
                and (entry.get('path') or '') == finding.get('path', '')
                and _same(str(entry.get('summary') or ''))
                == _same(finding['summary'][:PREVIOUS_SUMMARY_CHARS])):
            return entry.get('number')
    return None


def read_findings(output: Path, changed: set[str],
                  previous: list[dict[str, Any]] | None = None) -> list[ReviewFinding]:
    """The agent's output is untrusted: accept bounded, well-formed findings about the
    delivery's own changes only, each marked with the earlier finding it repeats."""
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
        repeats = _repeated(item, finding, previous or [])
        if repeats is not None:
            finding['repeats'] = repeats
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
    part = int(context.get(PART_KEY) or 0)
    context = {key: value for key, value in context.items()
               if key not in (ANSWERS_KEY, PART_KEY)}
    language = session_language(context.get('language'))
    session_dir = GENERATED_CODE_DIR / session_id
    artifact_dir = session_dir / 'review'
    paused = artifact_dir / docker_agents.PAUSED_DIR
    await run_sync(artifact_dir.mkdir, parents=True, exist_ok=True)
    async with async_context(tempfile.TemporaryDirectory) as temporary:
        work = Path(temporary)
        workspace = work / 'workspace'
        if answers:
            # The paused review continues in the workspace it left.
            await run_sync(docker_agents.resume_workspace, paused, workspace, answers)
            inputs = json.loads(await run_sync((workspace / 'input.json').read_text))
            changes = inputs.get('changes') or {}
            previous = inputs.get(PREVIOUS_KEY) or []
            count = int((inputs.get(PART_KEY) or {}).get('of') or 1)
        else:
            await run_sync(workspace.mkdir)
            changes, count = await run_sync(
                prepare_review, language, session_dir, context, workspace, part)
            previous = context.get(PREVIOUS_KEY) or []
        changed = {path for paths in changes.values() for path in paths}
        # The workflow runs the next part's review with this in its context.
        following = {'next_part': part + 1} if part + 1 < count else {}
        if not changed:
            await run_sync(shutil.rmtree, paused, ignore_errors=True)
            logger.info('Nothing to review (session=%s)', session_id)
            return {'session_id': session_id, 'findings': [], 'path': str(artifact_dir),
                    **following}
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
        findings = await run_sync(read_findings, output, changed, previous)
    result = {'session_id': session_id, 'findings': findings, 'path': str(artifact_dir)}
    manifest = artifact_dir / f'{uuid.uuid4().hex}.json'
    await run_sync(
        manifest.write_text, json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    name = FINDINGS if count == 1 else f'findings-part-{part + 1}.json'
    await run_sync(os.replace, manifest, artifact_dir / name)
    logger.info('Code review finished (session=%s, part=%d/%d, findings=%d)',
                session_id, part + 1, count, len(findings))
    return {**result, **following}


@activity.defn
async def review_changes(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    with trace_operation('review-changes', session_id=session_id, feature='review',
                         input=context.get('instructions'),
                         metadata=activity_metadata()) as span:
        result = await run_review(session_id, context)
        if span is not None:
            span.update(output=result)
        return result
