"""Host-only execution of the published service and independent HTTP test image."""
import asyncio
import contextlib
import fnmatch
import io
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app import docker_agents
from app.asyncio_utils import async_context, run_sync
from app.agent_workflows.host import build_image, publish_manifest, session_language
from app.github import work_refs
from app.github.checkout import (
    CHECKOUT_DIR, COMPONENT_SNAPSHOT, COMPONENT_TESTS_DIR, SNAPSHOT_DIR, TESTS_DIR,
)
from app.implementation.activities import copy_service
from app.review.activities import record_baseline
from app.test_generation.activities import copy_test_suite
from app.languages import (
    Language, TestReports, TestSuite, UnsupportedLanguage, get_language,
)
from app.languages.composite import CompositeSuite
from app.routing.parts import check_parts, component_parts
from app.test_generation.components import ARTIFACT_DIR as COMPONENT_TESTS_ARTIFACT
from app.test_generation.components import build_runner, delivery_suite, read_runners
from app.config import GENERATED_CODE_DIR
from app.verification.models import (
    COMPONENT_RUNS_DIR, TestFailure, TestSkip, TrackedTest, VerificationResult,
)
from app.temporal.tracing import trace_activity

logger = logging.getLogger(__name__)

MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
MAX_REPORT_FILES = 1000
MAX_TESTS = 100000
MAX_FAILURE_DETAILS = 50
MAX_TRACKED_TESTS = 1000
MAX_MISSING_TESTS = 50
LOG_LIMIT = 8000
CONSTRAINTS = (
    '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=256',
    '--memory=4g', '--cpus=2',
)


async def collect_reports(container: str, destination: Path, reports: TestReports) -> None:
    """Bound the Docker tar stream before touching any untrusted archive paths."""
    logger.info('Collecting test reports (container=%s, path=%s)', container, destination)
    async with async_context(tempfile.TemporaryFile) as stderr:
        process = await asyncio.create_subprocess_exec(
            'docker', 'cp', f'{container}:{reports.directory}/.', '-',
            stdout=asyncio.subprocess.PIPE, stderr=stderr,
        )
        try:
            async with asyncio.timeout(30):
                archive = bytearray()
                assert process.stdout is not None  # piped
                while chunk := await process.stdout.read(65536):
                    archive.extend(chunk)
                    if len(archive) > MAX_ARCHIVE_BYTES:
                        raise RuntimeError('Test report archive exceeds size limit')
                await process.wait()
                if process.returncode:
                    raise RuntimeError('Test reports are unavailable')
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    await run_sync(_extract_reports, archive, destination, reports)


def _extract_reports(archive: bytes | bytearray, destination: Path, reports: TestReports) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    # Docker emits an uncompressed tar. Do not auto-decompress untrusted input.
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as tar:
        names: set[str] = set()
        for member in tar:
            if member.issym() or member.islnk():
                raise RuntimeError('Linked test reports are not allowed')
            name = member.name.removeprefix('./')
            if not fnmatch.fnmatchcase(name, reports.pattern):
                continue
            if not member.isfile() or '/' in name or '\\' in name or name in names:
                raise RuntimeError('Invalid test report archive entry')
            names.add(name)
            if len(names) > MAX_REPORT_FILES or member.size > MAX_REPORT_BYTES:
                raise RuntimeError('Test reports exceed size limits')
            source = tar.extractfile(member)
            assert source is not None  # a regular file, checked above
            with source:
                (destination / name).write_bytes(source.read(MAX_REPORT_BYTES + 1))
        logger.info('Collected %d test reports', len(names))


def summarize_reports(directory: Path, reports: TestReports,
                      tracked: dict[str, TrackedTest] | None = None,
                      case_id: Callable[[str, str], str] | None = None,
                      suite_tests: set[str] | None = None,
                      part_of: Callable[[str], str] | None = None) -> dict:
    """Count JUnit XML testcase outcomes, not redundant nested suite summary attributes.
    The outcomes of the tests in `tracked`, keyed by `case_id`, are recorded in it, and
    the tests of `suite_tests` without any report entry are returned as missing. With
    `part_of`, which names the part of the repository a report entry's class name belongs
    to, the outcomes are counted by part as well, as `parts`."""
    totals = dict(tests=0, passed=0, failures=0, errors=0, skipped=0)
    parts: dict[str, dict[str, int]] = {}
    reported: set[str] = set()
    failures: list[TestFailure] = []
    skips: list[TestSkip] = []
    files = sorted(directory.glob(reports.pattern))
    if not files or len(files) > MAX_REPORT_FILES:
        raise RuntimeError('Valid test reports are unavailable')
    for file in files:
        if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_REPORT_BYTES:
            raise RuntimeError('Invalid or oversized test report')
        data = file.read_bytes()
        # ElementTree does not resolve external entities; reject declarations as well,
        # including UTF-16/32 encodings, to prevent internal entity expansion.
        declarations = data.replace(b'\x00', b'').upper()
        if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:
            raise RuntimeError('XML declarations are not allowed in test reports')
        root = ElementTree.fromstring(data)
        if root.tag not in {'testsuite', 'testsuites'}:
            raise RuntimeError('Invalid test report root')
        for case in root.iter('testcase'):
            if totals['tests'] >= MAX_TESTS:
                raise RuntimeError('Test count exceeds limit')
            outcomes = [child for child in case if child.tag in {'failure', 'error', 'skipped'}]
            if len(outcomes) > 1:
                raise RuntimeError('Conflicting test outcomes')
            outcome = outcomes[0] if outcomes else None
            key = ('passed' if outcome is None else 'skipped' if outcome.tag == 'skipped'
                   else 'failures' if outcome.tag == 'failure' else 'errors')
            totals['tests'] += 1
            totals[key] += 1
            if part_of:
                counts = parts.setdefault(part_of(case.get('classname', '')),
                                          dict(tests=0, passed=0, failures=0, errors=0,
                                               skipped=0))
                counts['tests'] += 1
                counts[key] += 1
            identity = case_id(case.get('classname', ''), case.get('name', '')) if case_id else ''
            reported.add(identity)
            watched = tracked.get(identity) if tracked else None
            if watched:
                watched['tests'] += 1
                watched['failed'] += key in ('failures', 'errors')
            if key == 'skipped' and outcome is not None and len(skips) < MAX_FAILURE_DETAILS:
                skips.append({
                    'class_name': case.get('classname', '')[:256],
                    'name': case.get('name', '')[:256],
                    'message': (outcome.get('message') or outcome.text or '')[:2000],
                })
            if key in ('failures', 'errors') and outcome is not None:
                kind: Literal['failure', 'error'] = (
                    'failure' if outcome.tag == 'failure' else 'error')
                if len(failures) < MAX_FAILURE_DETAILS:
                    failures.append({
                        'class_name': case.get('classname', '')[:256],
                        'name': case.get('name', '')[:256], 'kind': kind,
                        'message': (outcome.get('message') or outcome.text or '')[:2000],
                    })
    if not totals['tests'] or totals['tests'] == totals['skipped']:
        raise RuntimeError('No tests were executed')
    missing = None if suite_tests is None else sorted(suite_tests - reported)[:MAX_MISSING_TESTS]
    return {**totals, 'missing_tests': missing, 'test_failures': failures, 'test_skips': skips,
            'details_truncated': totals['failures'] + totals['errors'] > len(failures),
            **({'parts': parts} if part_of else {})}


def published_suite_tests(artifact_dir: Path, test_image: str, language: Language) -> set[str]:
    """Every test in the sources of the published suite that `test_image` was built from."""
    try:
        manifest = json.loads((artifact_dir / 'image.json').read_text())
    except (OSError, ValueError):
        manifest = None
    if not isinstance(manifest, dict) or manifest.get('image_id') != test_image:
        raise ApplicationError('The sources of the test image are unavailable', non_retryable=True)
    return set(language.test_suite.test_cases(artifact_dir / 'suite'))


async def cleanup(service: str, runner: str, network: str) -> None:
    for name in (runner, service):
        try:
            await docker_agents.docker('rm', '-f', name, timeout=30)
        except Exception:
            activity.logger.warning('Could not remove verification container %s', name)
    try:
        await docker_agents.docker('network', 'rm', network, timeout=30)
    except Exception:
        activity.logger.warning('Could not remove verification network %s', network)


async def run_verification(session_id: str, service_image: str, test_image: str,
                           language: str, tracked_tests: list[str] | None = None,
                           suite_dir: str = 'tests') -> VerificationResult:
    """`suite_dir` names the session artifact `test_image` was built from: the test
    author's, or the snapshot of the repository's own tests."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    if suite_dir not in ('tests', SNAPSHOT_DIR):
        raise ApplicationError('Invalid test suite', non_retryable=True)
    if not all(re.fullmatch(r'sha256:[0-9a-f]{64}', image) for image in (service_image, test_image)):
        raise ApplicationError('Invalid verification image ID', non_retryable=True)
    try:
        stack = get_language(language)
    except UnsupportedLanguage as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    reports = stack.test_reports
    if tracked_tests is not None and len(tracked_tests) > MAX_TRACKED_TESTS:
        raise ApplicationError('Too many tests to track', non_retryable=True)
    tracked: dict[str, TrackedTest] | None = None if tracked_tests is None else {
        item: {'tests': 0, 'failed': 0} for item in tracked_tests}
    suite_tests = None
    if tracked is not None:
        # A run that answers for single tests answers for the whole suite: its build
        # configuration could otherwise leave tests out without any of them failing.
        suite_tests = await run_sync(
            published_suite_tests, GENERATED_CODE_DIR / session_id / suite_dir, test_image, stack
        )
    token = uuid.uuid4().hex
    network = f'verification-{token}'
    service, runner = f'{network}-service', f'{network}-tests'
    artifact_dir = GENERATED_CODE_DIR / session_id / 'verification'
    run_dir = artifact_dir / token
    await run_sync(run_dir.mkdir, parents=True)
    result: VerificationResult = {
        'session_id': session_id, 'status': 'error',
        'tests': 0, 'passed': 0, 'failures': 0, 'errors': 0, 'skipped': 0,
        'duration_seconds': 0, 'exit_code': None, 'test_failures': [], 'test_skips': [],
        'details_truncated': False, 'tracked': tracked, 'missing_tests': None,
        'service_log': '', 'runner_log': '',
        'error': None, 'path': str(run_dir),
    }
    started = time.monotonic()
    logger.info('Verification starting (session=%s, service=%s, runner=%s, network=%s)',
                session_id, service, runner, network)
    created: set[str] = set()
    runner_started = False
    runner_state: dict = {}
    service_state: dict = {}
    try:
        async with docker_agents.keep_activity_alive('Verifying service with contract tests'):
            try:
                for image in (service_image, test_image):
                    user = await docker_agents.docker(
                        'image', 'inspect', '--format', '{{.Config.User}}', image, timeout=30,
                    )
                    account = user.split(':', 1)[0]
                    if not account or account == 'root' or (account.isdigit() and int(account) == 0):
                        raise RuntimeError('Verification images must declare a non-root runtime user')
                await docker_agents.docker('network', 'create', '--label',
                                           f'temporal.verification={session_id}', network, timeout=30)
                for name, image, options in (
                    (service, service_image, ('--network-alias', 'service')),
                    (runner, test_image, ('-e', 'SERVICE_BASE_URL=http://service:8080',
                                         '-e', 'SERVICE_WAIT_HOST=service', '-e', 'SERVICE_WAIT_PORT=8080')),
                ):
                    await docker_agents.docker(
                        'create', '--name', name, '--pull=never', '--network', network,
                        '--label', f'temporal.verification={session_id}', *CONSTRAINTS,
                        *options, image, timeout=30,
                    )
                    created.add(name)
                async with contextlib.AsyncExitStack() as forwarded:
                    for name, role in ((service, 'service'), (runner, 'tests')):
                        await docker_agents.docker('start', name, timeout=30)
                        runner_started = name == runner
                        await forwarded.enter_async_context(docker_agents.forward_logs(
                            name, f'verification {role} {session_id}'))
                    logger.info('Verification containers running (session=%s); waiting for readiness/tests', session_id)
                    # Includes readiness, test execution, and its forced-kill grace period.
                    await docker_agents.docker('wait', runner, timeout=750)
            except Exception as exc:
                result['error'] = str(exc)[-2000:]
                logger.error('Verification Docker execution failed (session=%s): %s',
                             session_id, result['error'])
            finally:
                for name, key in ((service, 'service_log'), (runner, 'runner_log')):
                    if name not in created:
                        continue
                    try:
                        log = await docker_agents.docker('logs', '--tail', '200', name, timeout=30)
                        result[key] = log[-LOG_LIMIT:]
                        await run_sync(
                            (run_dir / f'{key}.log').write_text, result[key], encoding='utf-8'
                        )
                    except Exception as exc:
                        result[key] = f'Could not retrieve logs: {str(exc)[-1000:]}'
                for name in created:
                    try:
                        state = json.loads(await docker_agents.docker(
                            'inspect', '--format', '{{json .State}}', name, timeout=30,
                        ))
                        if name == runner:
                            runner_state = state
                            result['exit_code'] = state['ExitCode']
                        else:
                            service_state = state
                    except Exception as exc:
                        result['error'] = result['error'] or f'Could not inspect container: {str(exc)[-1000:]}'
                if runner_started:
                    try:
                        await collect_reports(runner, run_dir / 'reports', reports)
                        result.update(
                            await run_sync(
                                summarize_reports,
                                run_dir / 'reports',
                                reports,
                                tracked,
                                stack.test_suite.case_id,
                                suite_tests,
                            )
                        )
                    except Exception as exc:
                        result['error'] = (
                            result['error'] or f'Could not read test reports: {str(exc)[-1000:]}'
                        )
            if runner_state.get('OOMKilled') or service_state.get('OOMKilled'):
                result['error'] = 'A verification container was OOM killed'
            elif result['exit_code'] in {124, 125, 137, 143}:
                result['error'] = 'Service readiness or test execution timed out or was terminated'
            elif service_state and not service_state.get('Running'):
                result['error'] = result['error'] or 'The service stopped during verification'
            if not result['error']:
                has_failures = result['failures'] + result['errors'] > 0
                if result['exit_code'] == 0 and not has_failures:
                    result['status'] = 'passed'
                elif result['exit_code'] == 1 and has_failures:
                    result['status'] = 'failed'
                else:
                    result['error'] = 'Runner exit status does not match the test reports'
    finally:
        # Shield resource removal from the original cancellation, but keep every
        # Docker operation bounded. Never publish a cancelled run as completed.
        task = asyncio.create_task(cleanup(service, runner, network))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
    result['duration_seconds'] = round(time.monotonic() - started, 3)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    await run_sync((run_dir / 'result.json').write_text, payload, encoding='utf-8')
    manifest = artifact_dir / f'{token}.json'
    await run_sync(manifest.write_text, payload, encoding='utf-8')
    await run_sync(os.replace, manifest, artifact_dir / 'result.json')
    logger.info('Verification finished (session=%s, status=%s, tests=%d, failures=%d, errors=%d, %.2fs, path=%s)',
                session_id, result['status'], result['tests'], result['failures'], result['errors'],
                result['duration_seconds'], run_dir)
    if result['error']:
        logger.error('Verification infrastructure error (session=%s): %s', session_id, result['error'])
    return result


@activity.defn
@trace_activity('verify-service', feature='verification', input_parameter='service_image')
async def verify_service(session_id: str, service_image: str, test_image: str,
                         language: str, tracked_tests: list[str] | None = None,
                         suite_dir: str = 'tests') -> VerificationResult:
    return await run_verification(session_id, service_image, test_image, language,
                                  tracked_tests, suite_dir)


@activity.defn
@trace_activity('verify-documentation', feature='verification', input_parameter='session_id')
async def verify_documentation(session_id: str) -> VerificationResult:
    """How a documentation task is verified: not at all yet. Nothing is started and the
    result says so, for the delivery to publish its documentation as unverified. The
    real check goes here."""
    return {'session_id': session_id, 'status': 'not_implemented', 'tests': 0, 'passed': 0,
            'failures': 0, 'errors': 0, 'skipped': 0, 'duration_seconds': 0.0,
            'exit_code': None, 'test_failures': [], 'test_skips': [], 'details_truncated': False,
            'tracked': None, 'missing_tests': None, 'service_log': '', 'runner_log': '',
            'error': None, 'path': ''}


async def _snapshot(suite: TestSuite, source: Path, target: Path) -> None:
    """Record the suite of the repository at `source` in `target`, once: the template's
    published files when the repository has none."""
    if await run_sync(target.is_dir):
        return
    await run_sync(target.parent.mkdir, parents=True, exist_ok=True)
    async with async_context(tempfile.TemporaryDirectory, dir=target.parent) as temporary:
        clean = Path(temporary) / target.name
        await run_sync(copy_test_suite, suite,
                       source if await run_sync(source.is_dir) else suite.template, clean)
        await run_sync(os.replace, clean, target)


@activity.defn
@trace_activity('snapshot-repository-tests', feature='verification', input_parameter='language')
async def snapshot_repository_tests(session_id: str, language: str) -> dict[str, int]:
    """Copy the repository's tests aside, as the checkout holds them before any author has
    run: its contract tests, its component tests in a language that has them, and those
    of each other part with a part language, under parts/P of the component tests'
    record, the layout of a suite of several parts, and the browser tests of each part
    whose web client has them, under browser/P of it. Each is taken once: a later call,
    such as a retried delivery's, keeps the first copy, whatever the checkout holds by
    then."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    registered = session_language(language)
    session_dir = GENERATED_CODE_DIR / session_id
    checkout = session_dir / CHECKOUT_DIR
    if not await run_sync(checkout.is_dir):
        raise ApplicationError('The repository checkout is unavailable', non_retryable=True)
    snapshot = session_dir / SNAPSHOT_DIR
    async with async_context(tempfile.TemporaryDirectory, dir=session_dir) as temporary:
        source = checkout
        if (not await run_sync((snapshot / 'suite').is_dir)
                and await work_refs.checkout_moved(session_dir)):
            # The record was lost, such as with this worker's disk, after the delivery
            # pushed to the checkout: it is taken again from the commit it started from.
            base = Path(temporary) / 'base'
            if not await work_refs.base_files(session_dir, base):
                raise ApplicationError(
                    "The repository's tests were not recorded, and the commit the session "
                    'started from is no longer available to record them from.',
                    non_retryable=True)
            logger.warning('Recording the repository tests of session %s again from the '
                           'commit it started from', session_id)
            source = base
        return await _record_suites(registered, session_id, source, snapshot)


async def _record_suites(registered: Language, session_id: str, checkout: Path,
                         snapshot: Path) -> dict[str, int]:
    # Each suite with where the checkout holds it, its count's key, its record and whether
    # it is counted only when it has tests, so a repository with no other part's tests is
    # recorded as before.
    suites = [(registered.test_suite, TESTS_DIR, 'tests', snapshot / 'suite', False)]
    if registered.component_suite:
        suites.append((registered.component_suite, COMPONENT_TESTS_DIR, 'component_tests',
                       snapshot / COMPONENT_SNAPSHOT, False))
    for part in (await run_sync(component_parts, registered, checkout))[1:]:
        assert part.suite is not None  # every part language has component tests
        suites.append((part.suite, part.tests_dir().as_posix(),
                       f'{"browser" if part.browser else "component"}_tests_of_{part.name}',
                       snapshot / COMPONENT_SNAPSHOT / part.snapshot_dir(), True))
    counts = {}
    for suite, directory, key, target, optional in suites:
        await _snapshot(suite, checkout / directory, target)
        cases = len(await run_sync(suite.test_cases, target))
        if cases or not optional:
            counts[key] = cases
    logger.info('Repository tests recorded (session=%s, %s)', session_id, counts)
    return counts


@activity.defn
@trace_activity('build-repository-tests', feature='verification', input_parameter='language')
async def build_repository_tests(session_id: str, language: str) -> dict[str, str]:
    """Build the runner of the repository's tests as the snapshot holds them, to run the
    tests an API change rewrote, as they were, against the changed service."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    session_language(language)
    artifact_dir = GENERATED_CODE_DIR / session_id / SNAPSHOT_DIR
    if not await run_sync((artifact_dir / 'suite').is_dir):
        raise ApplicationError('The repository tests were not recorded', non_retryable=True)
    async with docker_agents.keep_activity_alive('Building the repository tests'):
        async with async_context(tempfile.TemporaryDirectory, dir=artifact_dir) as temporary:
            clean = Path(temporary) / 'build'
            await run_sync(shutil.copytree, artifact_dir / 'suite', clean)
            image_id = await build_image(clean, 'repository test runner',
                                         artifact_dir / 'image-build.log')
    result = {'image_id': image_id, 'session_id': session_id}
    await run_sync(publish_manifest, artifact_dir, result)
    logger.info('Repository test image built (session=%s, image=%s)', session_id, image_id)
    return result


@activity.defn
@trace_activity('build-baseline', feature='verification', input_parameter='language')
async def build_baseline(session_id: str, language: str) -> dict[str, str]:
    """Build the service as the repository held it before the delivery: what the baseline
    run of a change executes the tests against. The service is recorded once, before any
    author ran, and every later baseline (a retry, a fix round after the delivery was
    pushed and the checkout updated) is built from that record, so a fix the delivery
    already pushed never makes its own regression tests pass the baseline."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    stack = session_language(language)
    session_dir = GENERATED_CODE_DIR / session_id
    checkout = session_dir / CHECKOUT_DIR
    if not await run_sync(checkout.is_dir):
        raise ApplicationError('The repository checkout is unavailable', non_retryable=True)
    artifact_dir = session_dir / 'baseline'
    await run_sync(artifact_dir.mkdir, parents=True, exist_ok=True)
    async with docker_agents.keep_activity_alive('Building the repository service'):
        async with async_context(tempfile.TemporaryDirectory, dir=artifact_dir) as temporary:
            clean = Path(temporary) / 'build'
            # The same record the code review compares the delivery's changes with.
            recorded = await run_sync(record_baseline, stack, session_dir)
            await run_sync(copy_service, stack, recorded, clean)
            if not await run_sync((clean / 'Dockerfile').is_file):
                raise ApplicationError('The repository has no Dockerfile to build its service',
                                       non_retryable=True)
            image_id = await build_image(clean, 'baseline service',
                                         artifact_dir / 'image-build.log')
    result = {'image_id': image_id, 'session_id': session_id}
    await run_sync(publish_manifest, artifact_dir, result)
    logger.info('Baseline service image built (session=%s, image=%s)', session_id, image_id)
    return result


# The image of the component tests built with the changed code, and the runs of either.
COMPONENT_RUNNER_DIR = 'component-verification'
# Exit statuses of a runner that was stopped, not one that reported.
STOPPED_EXIT_CODES = {124, 125, 137, 143}


@activity.defn
@trace_activity('build-component-runner', feature='verification', input_parameter='language')
async def build_component_runner(session_id: str, language: str) -> dict[str, str]:
    """Check that the changed code still conforms to the component contract, then build
    the component tests' runner with it. Returns the runner's `image_id`, or the
    `conformance_error` that says how the change broke the contract, which no run of the
    tests could then prove anything against."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    stack = session_language(language)
    session_dir = GENERATED_CODE_DIR / session_id
    parts, _ = await run_sync(delivery_suite, stack, session_dir)
    service = session_dir / 'implementation' / 'service'
    tests = session_dir / COMPONENT_TESTS_ARTIFACT / 'suite'
    for path, what in ((service, 'changed code'), (tests, 'component tests'),
                       *((part.routed_contract(session_dir), 'component contract')
                         for part in parts)):
        if not await run_sync(path.is_dir):
            raise ApplicationError(f'The {what} is unavailable', non_retryable=True)
    artifact_dir = session_dir / COMPONENT_RUNNER_DIR
    await run_sync(artifact_dir.mkdir, parents=True, exist_ok=True)
    async with docker_agents.keep_activity_alive('Building the component tests with the change'):
        async with async_context(tempfile.TemporaryDirectory, dir=artifact_dir) as temporary:
            work = Path(temporary)
            error = await check_parts(parts, session_dir, service, work, artifact_dir)
            if error:
                logger.info('Changed code breaks the component contract (session=%s)',
                            session_id)
                return {'session_id': session_id, 'conformance_error': error}
            await run_sync(shutil.copytree, tests, work / 'suite')
            try:
                image_id = await build_runner(stack, session_dir, work / 'suite',
                                              artifact_dir / 'image-build.log',
                                              repository=service)
            except RuntimeError as exc:
                # The contract holds, but the suite does not even collect with the change.
                return {'session_id': session_id, 'conformance_error': (
                    'The component tests could not be built with the changed code: '
                    f'{str(exc)[-2000:]}')}
    result = {'image_id': image_id, 'session_id': session_id}
    await run_sync(publish_manifest, artifact_dir, result)
    logger.info('Component test image built with the change (session=%s, image=%s)',
                session_id, image_id)
    return result


def _component_suite_tests(session_dir: Path, runner_image: str, suite: TestSuite) -> set[str]:
    """Every test of the component suite, when `runner_image` was built from it: by its
    author against the code the delivery started from, or with the changed code."""
    for directory in (COMPONENT_TESTS_ARTIFACT, COMPONENT_RUNNER_DIR):
        try:
            manifest = json.loads((session_dir / directory / 'image.json').read_text())
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict) and manifest.get('image_id') == runner_image:
            return set(suite.test_cases(session_dir / COMPONENT_TESTS_ARTIFACT / 'suite'))
    raise ApplicationError('The sources of the component test image are unavailable',
                           non_retryable=True)


def _empty_result(session_id: str, path: Path) -> VerificationResult:
    return {'session_id': session_id, 'status': 'error',
            'tests': 0, 'passed': 0, 'failures': 0, 'errors': 0, 'skipped': 0,
            'duration_seconds': 0, 'exit_code': None, 'test_failures': [], 'test_skips': [],
            'details_truncated': False, 'tracked': None, 'missing_tests': None,
            'service_log': '', 'runner_log': '', 'error': None, 'path': str(path)}


# How a browser test runner is confined beside the in-process runners' CONSTRAINTS:
# Chromium starts several processes and needs a larger /dev/shm.
BROWSER_CONSTRAINTS = (
    '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=1024',
    '--memory=4g', '--cpus=2', '--shm-size=512m',
)


def worst_exit(statuses: list[int | None]) -> int | None:
    """The exit status of several runners as one, as the composite runner's run-parts.sh
    gives it: a stopped run first, then any other error, then failed tests."""
    status: int | None = 0
    for item in statuses:
        if item is None:
            return None
        if item in STOPPED_EXIT_CODES:
            status = item
        elif item == 1:
            status = 1 if status == 0 else status
        elif item != 0 and status not in STOPPED_EXIT_CODES:
            status = item
    return status


def merge_reports(source: Path, destination: Path, pattern: str, index: int) -> None:
    """Move the reports of the runner at `index` (beside the first) into the directory of
    every runner's reports, renamed so that `pattern`, the service language's, matches
    them."""
    destination.mkdir(parents=True, exist_ok=True)
    for number, file in enumerate(sorted(source.iterdir())):
        name = pattern.replace('*', f'-runner-{index}-{number}', 1)
        if '*' in name or not fnmatch.fnmatchcase(name, pattern):
            raise RuntimeError('The reports of the browser tests cannot be named as the '
                               "service language's test reports are")
        os.replace(file, destination / name)


# How long a runner may take to its end: its own 600-second bound and its forced-kill
# grace period, with room to start.
RUNNER_SECONDS = 750
# How long all runners of one run may take together, within the workflow's 30-minute
# bound of the activity, leaving room to collect and read the reports. A run whose
# runners would take longer stops the next runner and reports it as stopped.
RUN_SECONDS = 1680
# The least time a runner is started with.
MIN_RUNNER_SECONDS = 120


async def _run_runner(session_id: str, image: str, browser: bool, reports: TestReports,
                      destination: Path, log_file: Path,
                      seconds: int = RUNNER_SECONDS) -> dict:
    """Run one runner image of the component tests to its end, without a network, and
    collect its reports into `destination`: its `exit_code`, `error`, `log` and
    `oom_killed`."""
    runner = f'component-tests-{uuid.uuid4().hex}'
    outcome: dict = {'exit_code': None, 'error': None, 'log': '', 'oom_killed': False}
    created = False
    try:
        try:
            user = await docker_agents.docker(
                'image', 'inspect', '--format', '{{.Config.User}}', image, timeout=30)
            account = user.split(':', 1)[0]
            if not account or account == 'root' or (account.isdigit() and int(account) == 0):
                raise RuntimeError('The component test image must declare a non-root '
                                   'runtime user')
            # In-process tests, or a browser against the client's own server on its
            # loopback: no service, and no network at all.
            await docker_agents.docker(
                'create', '--name', runner, '--pull=never', '--network=none',
                '--label', f'temporal.verification={session_id}',
                *(BROWSER_CONSTRAINTS if browser else CONSTRAINTS), image, timeout=30)
            created = True
            await docker_agents.docker('start', runner, timeout=30)
            async with docker_agents.forward_logs(
                    runner, f'{"browser" if browser else "component"} tests {session_id}'):
                # The runner's own timeout and its forced-kill grace period, within what
                # is left of the run's.
                await docker_agents.docker('wait', runner, timeout=seconds)
        except Exception as exc:
            outcome['error'] = str(exc)[-2000:]
            logger.error('Component test Docker execution failed (session=%s): %s',
                         session_id, outcome['error'])
        finally:
            if created:
                try:
                    log = await docker_agents.docker('logs', '--tail', '200', runner,
                                                     timeout=30)
                    outcome['log'] = log[-LOG_LIMIT:]
                    await run_sync(log_file.write_text, outcome['log'], encoding='utf-8')
                except Exception as exc:
                    outcome['log'] = f'Could not retrieve logs: {str(exc)[-1000:]}'
                try:
                    state = json.loads(await docker_agents.docker(
                        'inspect', '--format', '{{json .State}}', runner, timeout=30))
                    outcome['exit_code'] = state['ExitCode']
                    outcome['oom_killed'] = bool(state.get('OOMKilled'))
                except Exception as exc:
                    outcome['error'] = (outcome['error']
                                        or f'Could not inspect container: {str(exc)[-1000:]}')
                try:
                    await collect_reports(runner, destination, reports)
                except Exception as exc:
                    outcome['error'] = (outcome['error']
                                        or f'Could not read test reports: {str(exc)[-1000:]}')
    finally:
        if created:
            # Removed whatever cancelled the run, but bounded like every Docker operation.
            task = asyncio.create_task(docker_agents.docker('rm', '-f', runner, timeout=30))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            except Exception:
                activity.logger.warning('Could not remove component test container %s', runner)
    return outcome


@activity.defn
@trace_activity('run-component-suite', feature='verification', input_parameter='runner_image')
async def run_component_suite(session_id: str, runner_image: str, language: str,
                              tracked_tests: list[str]) -> VerificationResult:
    """Run the component tests in-process in their runner, one container without a
    network, and the browser tests of each part in theirs after it, and read their
    reports as one, as a verification reads the contract tests'. The run answers for
    every test of the suite, and for each of `tracked_tests` singly."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', runner_image):
        raise ApplicationError('Invalid component test image ID', non_retryable=True)
    if len(tracked_tests) > MAX_TRACKED_TESTS:
        raise ApplicationError('Too many tests to track', non_retryable=True)
    stack = session_language(language)
    session_dir = GENERATED_CODE_DIR / session_id
    _, suite = await run_sync(delivery_suite, stack, session_dir)
    suite_tests = await run_sync(_component_suite_tests, session_dir, runner_image, suite)
    runners = await run_sync(read_runners, session_dir, runner_image, stack.test_reports)
    tracked: dict[str, TrackedTest] = {item: {'tests': 0, 'failed': 0} for item in tracked_tests}
    token = uuid.uuid4().hex
    artifact_dir = session_dir / COMPONENT_RUNS_DIR
    run_dir = artifact_dir / token
    await run_sync(run_dir.mkdir, parents=True)
    result: VerificationResult = {**_empty_result(session_id, run_dir), 'tracked': tracked}
    started = time.monotonic()
    logger.info('Component tests starting (session=%s, runners=%d)', session_id, len(runners))
    outcomes: list[dict] = []
    async with docker_agents.keep_activity_alive('Running the component tests'):
        for index, runner in enumerate(runners):
            reports = TestReports(runner['reports']['directory'], runner['reports']['pattern'])
            browser = runner.get('member') is not None
            own = run_dir / ('reports' if index == 0 and reports == stack.test_reports
                             else f'reports-{index}')
            log_file = run_dir / ('runner_log.log' if index == 0 else f'runner_log-{index}.log')
            left = int(RUN_SECONDS - (time.monotonic() - started))
            if left < MIN_RUNNER_SECONDS:
                outcomes.append({'exit_code': 124, 'error': None, 'log': '',
                                 'oom_killed': False})
                logger.warning('Component tests: no time is left for runner %d of %d '
                               '(session=%s)', index + 1, len(runners), session_id)
                break
            if browser:
                # Built on it; a worker that lost it since pulls it again.
                try:
                    await docker_agents.ensure_image(docker_agents.PLAYWRIGHT_IMAGE)
                except docker_agents.ImageUnavailable as exc:
                    outcomes.append({'exit_code': None, 'error': str(exc), 'log': '',
                                     'oom_killed': False})
                    break
            outcome = await _run_runner(session_id, runner['image'], browser, reports, own,
                                        log_file, min(RUNNER_SECONDS, left))
            outcomes.append(outcome)
            if own.name != 'reports' and await run_sync(own.is_dir):
                await run_sync(merge_reports, own, run_dir / 'reports',
                               stack.test_reports.pattern, index)
    result['runner_log'] = '\n'.join(item['log'] for item in outcomes if item['log'])[-LOG_LIMIT:]
    result['exit_code'] = worst_exit([item['exit_code'] for item in outcomes])
    result['error'] = next((item['error'] for item in outcomes if item['error']), None)
    try:
        result.update(await run_sync(
            summarize_reports, run_dir / 'reports', stack.test_reports,
            tracked, suite.case_id, suite_tests,
            # A suite of more than the service counts each part's tests.
            suite.part_of if isinstance(suite, CompositeSuite) else None))
    except Exception as exc:
        result['error'] = (result['error']
                           or f'Could not read test reports: {str(exc)[-1000:]}')
    if any(item['oom_killed'] for item in outcomes):
        result['error'] = 'The component test container was OOM killed'
    elif result['exit_code'] in STOPPED_EXIT_CODES:
        # The runner reports the tests that finished before it was stopped.
        result['error'] = 'The component tests timed out or were terminated' + (
            f" after {result['tests']} of {len(suite_tests)} tests finished"
            if result['tests'] else '')
    if not result['error']:
        has_failures = result['failures'] + result['errors'] > 0
        if result['exit_code'] == 0 and not has_failures:
            result['status'] = 'passed'
        elif result['exit_code'] == 1 and has_failures:
            result['status'] = 'failed'
        else:
            result['error'] = 'Runner exit status does not match the test reports'
    result['duration_seconds'] = round(time.monotonic() - started, 3)
    await run_sync((run_dir / 'result.json').write_text,
                   json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info('Component tests finished (session=%s, status=%s, tests=%d, failures=%d, '
                'errors=%d, %.2fs, path=%s)', session_id, result['status'], result['tests'],
                result['failures'], result['errors'], result['duration_seconds'], run_dir)
    return result
