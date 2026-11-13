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
from functools import partial
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app import config, docker_agents
from app.asyncio_utils import async_context, run_sync
from app.agent_workflows.contracts import on_demand_contracts, write_contracts
from app.agent_workflows.host import (
    ANSWERS_KEY, RESUME_KEY, limited_result, copy_regular_files, paused_result, session_language, write_language,
)
from app.config import GENERATED_CODE_DIR
from app.github.checkout import (
    CHECKOUT_DIR, COMPONENT_SNAPSHOT, COMPONENT_TESTS_DIR, SNAPSHOT_DIR,
)
from app.implementation.activities import SEAM_DIR, checkout_parts, copy_service
from app.languages import RUNTIME_FILE, Language
from app.requirements.document import without_repeated_tables
from app.routing.parts import component_suite, read_routed_parts
from app.test_generation.activities import copy_test_suite
from app.review import checks, settled
from app.review.agent import FINDINGS
from app.review.models import (
    MAX_FINDINGS, PATH_CHARS, PREVIOUS_SUMMARY_CHARS, REASON_CHARS, REVIEW_TARGETS,
    BASELINE_KEY, SCOPE_KEY, SCOPE_TARGETS, SUMMARY_CHARS, REVIEW_INSTRUCTIONS_CHARS, ReviewFinding,
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
# The changed files an earlier round judged clean, which the run does not review, in its input.
SETTLED_KEY = 'settled_changes'
# The static tools' results on the changed lines, in the delivery review's input.
STATIC_KEY = 'static_findings'
# What the host decided in place of a review agent, in a run's result.
CHECKED_BY = 'checked_by'
# The report a static tool writes in its container.
STATIC_REPORT = '/tmp/static-report.json'
STATIC_TIMEOUT = 600


@dataclasses.dataclass
class PreparedReview:
    """A review run as the host prepared it."""
    # The changed files of each side that the run reviews.
    changes: dict[str, list[str]]
    # The parts the round's changes are reviewed in.
    count: int
    # The diff of each file the run reviews.
    own: dict[str, str] = dataclasses.field(default_factory=dict)
    # What the reviewer judges the files by besides their diffs (settled.judgement_key).
    key: str = ''
    # Findings a host check decided in place of the agent, which then does not run; an
    # empty list when the check found the changes certainly right. None otherwise.
    decided: list[ReviewFinding] | None = None
    # The check that decided.
    checked_by: str = ''


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


def _unavailable() -> ApplicationError:
    return ApplicationError('The sources under review are unavailable', non_retryable=True)


def _routed(language: Language, session_dir: Path) -> list[Any]:
    try:
        return read_routed_parts(language, session_dir)
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc


def _contract_changes(language: Language, session_dir: Path, review: Path) -> dict[str, str]:
    """The delivery's contract tests, against the repository's as recorded, or the
    language's template when it had none."""
    suite = session_dir / 'tests/suite'
    if not suite.is_dir():
        raise _unavailable()
    snapshot = session_dir / SNAPSHOT_DIR / 'suite'
    tests_before = snapshot if snapshot.is_dir() else language.test_suite.template
    copy_regular_files(suite, review / 'tests', include=lambda relative: True)
    return changed_files(tests_before, review / 'tests', 'tests')


def _component_changes(language: Language, session_dir: Path, review: Path,
                       routed: list[Any]) -> dict[str, str]:
    """The delivery's component tests, against the repository's as recorded."""
    tests = session_dir / COMPONENT_TESTS_ARTIFACT / 'suite'
    if not tests.is_dir():
        raise _unavailable()
    component_tests = component_suite(language, routed)
    copy_regular_files(tests, review / COMPONENT_TESTS_DIR, include=lambda relative: True)
    with tempfile.TemporaryDirectory() as temporary:
        # Recorded before any author ran, as the template's files when the repository
        # had no component tests: of the parts the suite has a project for alone.
        before = Path(temporary) / 'before'
        recorded = session_dir / SNAPSHOT_DIR / COMPONENT_SNAPSHOT
        if recorded.is_dir():
            copy_test_suite(component_tests, recorded, before)
        return changed_files(before, review / COMPONENT_TESTS_DIR, COMPONENT_TESTS_DIR)


def _copy_stubs(session_dir: Path, review: Path, routed: list[Any]) -> list[tuple[Path, Path]]:
    """The stubs the routing checked of each part, where the repository has them, copied
    for the reviewer to read; returns each with its published path."""
    stubs = [(item.routed_contract(session_dir), item.published_dir()) for item in routed]
    if not all(kept.is_dir() for kept, _ in stubs):
        raise _unavailable()
    for kept, published in stubs:
        copy_regular_files(kept, review / published, include=lambda relative: True)
    return stubs


def _delivery_changes(language: Language, session_dir: Path, context: dict[str, Any],
                      review: Path) -> tuple[dict[str, str], list[str], dict[str, Any]]:
    """The verified delivery: the service and its tests after it. A change with criteria
    routed to seams (`component_routes` in the context) also has its component tests
    reviewed, and the stubs of its component contract, which no author of the delivery
    may change; it has contract tests of its own only when `contract_tests` says so."""
    component = bool(context.get('component_routes'))
    contract = not component or bool(context.get('contract_tests'))
    service = session_dir / 'implementation/service'
    if not service.is_dir():
        raise _unavailable()
    service_before = record_baseline(language, session_dir)
    copy_service(language, service, review / 'service')
    diffs = changed_files(service_before, review / 'service', 'service')
    sides = ['service']
    if contract:
        diffs.update(_contract_changes(language, session_dir, review))
        sides.append('tests')
    if component:
        routed = _routed(language, session_dir)
        diffs.update(_component_changes(language, session_dir, review, routed))
        sides.append(COMPONENT_TESTS_DIR)
        for _, published in _copy_stubs(session_dir, review, routed):
            # The stubs the repository published, which the routing reused or extended.
            diffs.update(changed_files(session_dir / CHECKOUT_DIR / published,
                                       review / published, published.as_posix()))
            sides.append(published.as_posix())
    return diffs, sides, ({'contract_tests': contract} if component else {})


def _seam_changes(language: Language, session_dir: Path,
                  review: Path) -> tuple[dict[str, str], list[str], dict[str, Any]]:
    """The code with the seam a change added before its tests, against the repository's
    code as recorded. The stubs it has to match are beside it, to read only."""
    service = session_dir / SEAM_DIR / 'service'
    if not service.is_dir():
        raise _unavailable()
    service_before = record_baseline(language, session_dir)
    copy_service(language, service, review / 'service')
    _copy_stubs(session_dir, review, _routed(language, session_dir))
    return changed_files(service_before, review / 'service', 'service'), ['service'], {}


def _regression_changes(language: Language, session_dir: Path, context: dict[str, Any],
                        review: Path) -> tuple[dict[str, str], list[str], dict[str, Any]]:
    """A change's regression tests before the fix: the suites its authors wrote, against
    the repository's as recorded. The code they ran against (the repository's, or with
    the seam the change added) and the stubs are beside them, to read only."""
    seam = session_dir / SEAM_DIR / 'service'
    service = seam if context.get('seam_prepared') else record_baseline(language, session_dir)
    if not service.is_dir():
        raise _unavailable()
    copy_service(language, service, review / 'service')
    component = bool(context.get('component_routes'))
    contract = not component or bool(context.get('contract_tests'))
    diffs: dict[str, str] = {}
    sides = []
    if contract:
        diffs.update(_contract_changes(language, session_dir, review))
        sides.append('tests')
    if component:
        routed = _routed(language, session_dir)
        diffs.update(_component_changes(language, session_dir, review, routed))
        sides.append(COMPONENT_TESTS_DIR)
        _copy_stubs(session_dir, review, routed)
    return diffs, sides, ({'contract_tests': contract} if component else {})


def _criteria_coverage(context: dict[str, Any], diffs: dict[str, str], sides: list[str],
                       ) -> tuple[list[ReviewFinding], dict[str, list[str]]] | None:
    """The requirement-tag check of a regression review; None without criteria."""
    ids = [str(item.get('id')) for item in (context.get('brief') or {}).get('requirements', [])
           if isinstance(item, dict) and item.get('id')]
    if not ids:
        return None
    return checks.requirement_coverage(
        diffs, ids, list(context.get('component_routes') or {}),
        contract='tests' in sides, component=COMPONENT_TESTS_DIR in sides)


def _seam_verdict(language: Language, session_dir: Path, context: dict[str, Any],
                  review: Path, diffs: dict[str, str]) -> list[ReviewFinding] | None:
    """The host's verdict on a seam of the service: its certain violations, or no finding
    when it is certainly right. None when the agent has to judge it, as for a seam of
    another part or of a language whose sources the host cannot locate."""
    contract = language.component_contract
    seam = context.get('seam')
    if contract is None or contract.source_paths is None or not isinstance(seam, dict):
        return None
    routes = context.get('component_routes') or {}
    if any(isinstance(route, dict) and (route.get('seam') or {}).get('part')
           for route in routes.values()):
        return None
    try:
        service = next(item for item in _routed(language, session_dir) if item.is_service)
        runtime = json.loads(
            (service.routed_contract(session_dir) / RUNTIME_FILE).read_text(encoding='utf-8'))
        modules = {module: str(module) for module in seam}
        allowed = {path: module for module in modules
                   for path in contract.source_paths(module, runtime)}
    except (StopIteration, ApplicationError, OSError, ValueError, KeyError, TypeError):
        return None
    findings, certain = checks.seam_check(
        session_dir / BASELINE_DIR / 'service', review / 'service', diffs, allowed,
        language.build_dirs)
    return findings if findings else [] if certain else None


def plan_review(language: Language, session_dir: Path, context: dict[str, Any],
                workspace: Path, part: int = 0, *, record: list[dict[str, Any]] | None = None,
                round_id: str = '') -> PreparedReview:
    """The trees under review, the changes of the given part and the contracts; nothing
    else. By default the verified delivery is reviewed; a stage review (SCOPE_KEY in the
    context) reviews the seam a change added first, or its regression tests before the
    fix. A later round (PREVIOUS_KEY in the context) leaves out the files `record` holds
    as settled. The host's own checks may decide the round instead of the agent."""
    review = workspace / 'review'
    scope = context.get(SCOPE_KEY)
    if scope == 'seam':
        diffs, sides, own_input = _seam_changes(language, session_dir, review)
    elif scope == 'regression':
        diffs, sides, own_input = _regression_changes(language, session_dir, context, review)
    else:
        diffs, sides, own_input = _delivery_changes(language, session_dir, context, review)
    contracts = without_repeated_tables(
        {key: value for key, value in context.items()
         if key not in ('instructions', 'repository', 'language', 'contract_tests',
                        PREVIOUS_KEY, SCOPE_KEY, BASELINE_KEY)})
    instructions = str(context.get('instructions') or '')[:REVIEW_INSTRUCTIONS_CHARS]
    key = settled.judgement_key(scope, instructions, contracts)

    def by_side(paths: list[str]) -> dict[str, list[str]]:
        return {side: [path for path in paths if path.startswith(f'{side}/')][:LISTED_CHANGES]
                for side in sides}

    checked: dict[str, Any] = {}
    if scope == 'regression' and config.REVIEW_REQUIREMENT_TAGS:
        coverage = _criteria_coverage(context, diffs, sides)
        if coverage and coverage[0] and part == 0:
            # The tests are about to change: reviewing them now would be spent twice.
            return PreparedReview(by_side(list(diffs)), 1, key=key, decided=coverage[0],
                                  checked_by='requirement-tags')
        if coverage:
            checked['criteria_tests'] = coverage[1]
    if scope == 'seam' and config.REVIEW_SEAM_CHECKS and part == 0:
        verdict = _seam_verdict(language, session_dir, context, review, diffs)
        if verdict is not None:
            return PreparedReview(by_side(list(diffs)), 1, key=key, decided=verdict,
                                  checked_by='seam')
    done = (settled.settled_files(record or [], diffs, key, round_id)
            if config.REVIEW_SKIP_SETTLED and context.get(PREVIOUS_KEY) else set())
    reviewable = {path: diff for path, diff in diffs.items() if path not in done}
    parts = review_parts(reviewable)
    if part >= max(len(parts), 1):
        raise ApplicationError(f'The changes have no review part {part}', non_retryable=True)
    own = parts[part] if parts else []
    diff = ''.join(reviewable[path] for path in own)
    if len(diff) > MAX_DIFF_CHARS:
        diff = (diff[:MAX_DIFF_CHARS]
                + '\n[The diff is cut here; read the remaining changed files directly.]\n')
    (review / 'changes.diff').write_text(diff, encoding='utf-8')

    changes = by_side(own)
    others = [path for path in reviewable if path not in own]
    index, files = on_demand_contracts(contracts, len(json.dumps(contracts))) or (contracts, {})
    write_language(workspace, language, label=language.label)
    if files:
        write_contracts(workspace, files)
    # An onboarding changes no part of the repository but the service's Dockerfile.
    checkout = session_dir / CHECKOUT_DIR
    projects = (checkout_parts(language, checkout)
                if checkout.is_dir() and not context.get('onboarding') else [])
    (workspace / 'input.json').write_text(json.dumps(
        {**index, 'changes': changes,
         **own_input,
         **({SCOPE_KEY: scope} if scope else {}),
         # How the regression tests failed without the fix, which a regression review
         # judges them by.
         **({BASELINE_KEY: context[BASELINE_KEY]}
            if scope == 'regression' and context.get(BASELINE_KEY) else {}),
         **({'repository_parts': projects} if projects else {}),
         **({PART_KEY: {'part': part + 1, 'of': len(parts), 'other_changes': by_side(others)}}
            if len(parts) > 1 else {}),
         # Judged clean by an earlier round and unchanged since: there to read only.
         **({SETTLED_KEY: by_side(sorted(done))} if done else {}),
         **checked,
         **({'instructions': instructions} if instructions else {}),
         **({PREVIOUS_KEY: context[PREVIOUS_KEY]} if context.get(PREVIOUS_KEY) else {})},
        ensure_ascii=False))
    return PreparedReview(changes, max(len(parts), 1),
                          own={path: reviewable[path] for path in own}, key=key)


def prepare_review(language: Language, session_dir: Path, context: dict[str, Any],
                   workspace: Path, part: int = 0) -> tuple[dict[str, list[str]], int]:
    """`plan_review` without a record of settled files: the changed files of each side in
    the part, and the number of parts."""
    prepared = plan_review(language, session_dir, context, workspace, part)
    return prepared.changes, prepared.count


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
                  previous: list[dict[str, Any]] | None = None,
                  target: str | None = None) -> list[ReviewFinding]:
    """The agent's output is untrusted: accept bounded, well-formed findings about the
    delivery's own changes only, each marked with the earlier finding it repeats. A
    stage review has one worker, `target`, to correct whatever it finds."""
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
        if target:
            item = {**item, 'target': target}
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


def _round_id() -> str:
    """The review round in progress: the child workflow running it, one per round."""
    return activity.info().workflow_id if activity.in_activity() else ''


async def _static_findings(language: Language, session_id: str, workspace: Path,
                           own: dict[str, str], artifact_dir: Path) -> list[dict[str, Any]]:
    """The language's static tools over the trees under review, in its agent image without
    a network, cut to the lines the run's changes add. A tool that fails is left out: the
    reviewer then looks for those problems itself, as without the tools."""
    results: list[dict[str, Any]] = []
    image = dataclasses.replace(docker_agents.default_agent_image('review', 'review'),
                                dockerfile=language.agent_dockerfile)
    for check in language.static_checks:
        try:
            report = await docker_agents.run_tool(
                'review', session_id, workspace, check.command, STATIC_REPORT,
                artifact_dir / f'static-{check.name}.log', image_build=image,
                timeout=STATIC_TIMEOUT)
        except Exception:
            logger.warning('The static check %s could not run (session=%s)', check.name,
                           session_id, exc_info=True)
            continue
        results += [{**item, 'tool': check.name} for item in check.parse(report)]
    return checks.changed_static_findings(results, own)


async def _add_input(workspace: Path, values: dict[str, Any]) -> None:
    file = workspace / 'input.json'
    inputs = json.loads(await run_sync(file.read_text))
    await run_sync(file.write_text, json.dumps({**inputs, **values}, ensure_ascii=False))


async def _save_result(artifact_dir: Path, name: str, result: dict[str, Any]) -> None:
    manifest = artifact_dir / f'{uuid.uuid4().hex}.json'
    await run_sync(
        manifest.write_text, json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    await run_sync(os.replace, manifest, artifact_dir / name)


async def run_review(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    """The findings, or `questions` the agent asked: then it is paused, and the same call
    with ANSWERS_KEY in the context continues it."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    answers = context.get(ANSWERS_KEY)
    # An agent its provider's limits stopped continues where it was, as a paused one does.
    resume = bool(context.get(RESUME_KEY))
    part = int(context.get(PART_KEY) or 0)
    context = {key: value for key, value in context.items()
               if key not in (ANSWERS_KEY, RESUME_KEY, PART_KEY)}
    language = session_language(context.get('language'))
    session_dir = GENERATED_CODE_DIR / session_id
    # A stage review keeps its findings and paused agent apart from the delivery's review;
    # a resumed review is given the same context, its scope included.
    scope = context.get(SCOPE_KEY)
    if scope is not None and scope not in SCOPE_TARGETS:
        raise ApplicationError(f'Unknown review scope {scope!r}', non_retryable=True)
    artifact_dir = session_dir / ('review' if scope is None else f'review-{scope}')
    paused = artifact_dir / docker_agents.PAUSED_DIR
    target = SCOPE_TARGETS.get(scope) if scope else None
    await run_sync(artifact_dir.mkdir, parents=True, exist_ok=True)
    async with async_context(tempfile.TemporaryDirectory) as temporary:
        work = Path(temporary)
        workspace = work / 'workspace'
        if answers or resume:
            # The paused review continues in the workspace it left.
            await run_sync(docker_agents.resume_workspace, paused, workspace,
                           answers or None)
            inputs = json.loads(await run_sync((workspace / 'input.json').read_text))
            changes = inputs.get('changes') or {}
            previous = inputs.get(PREVIOUS_KEY) or []
            count = int((inputs.get(PART_KEY) or {}).get('of') or 1)
        else:
            await run_sync(workspace.mkdir)
            previous = context.get(PREVIOUS_KEY) or []
            prepared = await run_sync(
                partial(plan_review, record=await run_sync(settled.read_record, artifact_dir),
                        round_id=_round_id()),
                language, session_dir, context, workspace, part)
            if prepared.decided is not None:
                # A host check decided the round: no agent runs, and no other part.
                findings = [{**item, **({'target': target} if target else {})}
                            for item in prepared.decided]
                for finding in findings:
                    repeats = _repeated({}, finding, previous)
                    if repeats is not None:
                        finding['repeats'] = repeats
                result = {'session_id': session_id, 'findings': findings[:MAX_FINDINGS],
                          'path': str(artifact_dir), CHECKED_BY: prepared.checked_by}
                await _save_result(artifact_dir, FINDINGS, result)
                logger.info('Review decided by the %s check (session=%s, findings=%d)',
                            prepared.checked_by, session_id, len(findings))
                return result
            changes, count = prepared.changes, prepared.count
            await run_sync(settled.write_pending, artifact_dir, part, _round_id(),
                           prepared.key, prepared.own)
            if (scope is None and config.REVIEW_STATIC_CHECKS and language.static_checks
                    and prepared.own):
                static = await _static_findings(language, session_id, workspace,
                                                prepared.own, artifact_dir)
                if static:
                    await _add_input(workspace, {STATIC_KEY: static})
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
            except docker_agents.AgentLimited as exc:
                return limited_result(session_id, exc)
        findings = await run_sync(read_findings, output, changed, previous, target)
    await run_sync(settled.record_run, artifact_dir, part, findings)
    result = {'session_id': session_id, 'findings': findings, 'path': str(artifact_dir)}
    await _save_result(artifact_dir, FINDINGS if count == 1 else f'findings-part-{part + 1}.json',
                       result)
    logger.info('Code review finished (session=%s, part=%d/%d, findings=%d)',
                session_id, part + 1, count, len(findings))
    return {**result, **following}


@activity.defn
async def review_changes(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    # The scope and part let traces be grouped by them (plans/deterministic-checks.md).
    metadata = {**activity_metadata(), 'review_scope': str(context.get(SCOPE_KEY) or 'review'),
                'review_part': str(int(context.get(PART_KEY) or 0) + 1)}
    with trace_operation('review-changes', session_id=session_id, feature='review',
                         input=context.get('instructions'), metadata=metadata) as span:
        result = await run_review(session_id, context)
        if span is not None:
            span.update(output=result)
        return result
