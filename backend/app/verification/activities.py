"""Host-only execution of the published service and independent HTTP test image."""
import asyncio
import fnmatch
import io
import json
import logging
import os
import re
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from xml.etree import ElementTree

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app import docker_agents
from app.languages import TestReports, UnsupportedLanguage, get_language
from app.config import GENERATED_CODE_DIR
from app.verification.models import TestFailure, VerificationResult
from app.temporal.tracing import trace_activity

logger = logging.getLogger(__name__)

MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
MAX_REPORT_FILES = 1000
MAX_TESTS = 100000
MAX_FAILURE_DETAILS = 50
LOG_LIMIT = 8000
CONSTRAINTS = (
    '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=256',
    '--memory=4g', '--cpus=2',
)


async def collect_reports(container: str, destination: Path, reports: TestReports) -> None:
    """Bound the Docker tar stream before touching any untrusted archive paths."""
    logger.info('Collecting test reports (container=%s, path=%s)', container, destination)
    with tempfile.TemporaryFile() as stderr:
        process = await asyncio.create_subprocess_exec(
            'docker', 'cp', f'{container}:{reports.directory}/.', '-',
            stdout=asyncio.subprocess.PIPE, stderr=stderr,
        )
        try:
            async with asyncio.timeout(30):
                archive = bytearray()
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
            with tar.extractfile(member) as source:
                (destination / name).write_bytes(source.read(MAX_REPORT_BYTES + 1))
        logger.info('Collected %d test reports (container=%s)', len(names), container)


def summarize_reports(directory: Path, reports: TestReports) -> dict:
    """Count JUnit XML testcase outcomes, not redundant nested suite summary attributes."""
    totals = dict(tests=0, passed=0, failures=0, errors=0, skipped=0)
    failures: list[TestFailure] = []
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
            totals['tests'] += 1
            if totals['tests'] > MAX_TESTS:
                raise RuntimeError('Test count exceeds limit')
            outcomes = [child for child in case if child.tag in {'failure', 'error', 'skipped'}]
            if len(outcomes) > 1:
                raise RuntimeError('Conflicting test outcomes')
            outcome = outcomes[0] if outcomes else None
            if outcome is None:
                totals['passed'] += 1
            elif outcome.tag == 'skipped':
                totals['skipped'] += 1
            else:
                kind = outcome.tag
                totals['failures' if kind == 'failure' else 'errors'] += 1
                if len(failures) < MAX_FAILURE_DETAILS:
                    failures.append({
                        'class_name': case.get('classname', '')[:256],
                        'name': case.get('name', '')[:256], 'kind': kind,
                        'message': (outcome.get('message') or outcome.text or '')[:2000],
                    })
    if not totals['tests'] or totals['tests'] == totals['skipped']:
        raise RuntimeError('No tests were executed')
    return {**totals, 'test_failures': failures,
            'details_truncated': totals['failures'] + totals['errors'] > len(failures)}


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
                           language: str) -> VerificationResult:
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    if not all(re.fullmatch(r'sha256:[0-9a-f]{64}', image) for image in (service_image, test_image)):
        raise ApplicationError('Invalid verification image ID', non_retryable=True)
    try:
        reports = get_language(language).test_reports
    except UnsupportedLanguage as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    token = uuid.uuid4().hex
    network = f'verification-{token}'
    service, runner = f'{network}-service', f'{network}-tests'
    artifact_dir = GENERATED_CODE_DIR / session_id / 'verification'
    run_dir = artifact_dir / token
    run_dir.mkdir(parents=True)
    result: VerificationResult = {
        'session_id': session_id, 'status': 'error',
        'tests': 0, 'passed': 0, 'failures': 0, 'errors': 0, 'skipped': 0,
        'duration_seconds': 0, 'exit_code': None, 'test_failures': [],
        'details_truncated': False, 'service_log': '', 'runner_log': '',
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
                await docker_agents.docker('start', service, timeout=30)
                await docker_agents.docker('start', runner, timeout=30)
                runner_started = True
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
                        (run_dir / f'{key}.log').write_text(result[key], encoding='utf-8')
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
                        result.update(summarize_reports(run_dir / 'reports', reports))
                    except Exception as exc:
                        result['error'] = result['error'] or f'Could not read test reports: {str(exc)[-1000:]}'
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
    (run_dir / 'result.json').write_text(payload, encoding='utf-8')
    manifest = artifact_dir / f'{token}.json'
    manifest.write_text(payload, encoding='utf-8')
    os.replace(manifest, artifact_dir / 'result.json')
    logger.info('Verification finished (session=%s, status=%s, tests=%d, failures=%d, errors=%d, %.2fs, path=%s)',
                session_id, result['status'], result['tests'], result['failures'], result['errors'],
                result['duration_seconds'], run_dir)
    if result['error']:
        logger.error('Verification infrastructure error (session=%s): %s', session_id, result['error'])
    return result


@activity.defn
@trace_activity('verify-service', feature='verification', input_parameter='service_image')
async def verify_service(session_id: str, service_image: str, test_image: str,
                         language: str) -> VerificationResult:
    return await run_verification(session_id, service_image, test_image, language)
