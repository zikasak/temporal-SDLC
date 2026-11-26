"""Host-side Docker lifecycle for isolated coding agents; never mounted in agents."""

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.asyncio_utils import async_context, run_sync
from app.agent_workflows.runtime import (
    ANSWERS_FILENAME, INTERRUPTED_EXIT, PAUSE_EXIT, STATE_FILENAME,
)
# The images pulled ahead of use, by digest; the languages build their runners on them.
from app.images import PLAYWRIGHT_IMAGE, PREPULLED_IMAGES  # noqa: F401
from app.llm.limits import LIMIT_FILENAME, MESSAGE_CHARS, PROVIDER_LIMIT_EXIT
from app.config import PI_CREDENTIALS_DIR, PI_CREDENTIALS_SHARED
from app.llm.factory import NO_API_KEY
from app.llm.questions import Question, normalize_questions
from app.settings.models import ModelSettings, SETTINGS_FILENAME, WORKFLOW_LABELS, WorkflowType
from app.settings.endpoint import OpenAIEndpoint, get_openai_endpoint
from app.settings.storage import get_workflow_settings
from app.tracing import container_trace_environment

logger = logging.getLogger(__name__)
# Output of sandboxed containers, re-logged line by line at each line's own level.
container_logger = logging.getLogger('app.containers')
# Forwarded by name; LOG_LEVEL sets the agent's own logging, which forward_logs re-logs.
# The *_MAX_TURNS budgets are read inside the container by each stage's AgentProfile.
AGENT_SETTINGS = ('LOG_LEVEL', 'IMPLEMENTATION_MAX_TURNS',
                  'TEST_GENERATION_MAX_TURNS', 'REVIEW_MAX_TURNS', 'DIAGNOSIS_MAX_TURNS',
                  'DOCUMENTATION_MAX_TURNS', 'ROUTING_MAX_TURNS')
CONTAINER_LINE_CHARS = 4000
# How long a container's remaining output may take to arrive once it was waited for.
LOG_DRAIN_SECONDS = 2
# The longest an agent may run.
AGENT_TIMEOUT_SECONDS = 3600
# How long an agent that ran out of time or was cancelled (a worker shutting down for a
# redeploy, say) has, once asked to stop, to export its traces
# (app.llm.claude.TRACE_EXPORT_TIMEOUT or app.llm.pi.SHUTDOWN_TIMEOUT, then the tracing
# flush) before it is killed.
AGENT_STOP_GRACE_SECONDS = 150
# A level name near the start of a line: Python's `INFO name: ...`, Logback's
# `12:00:00.000 [main] WARN ...`, Spring Boot's `<timestamp>  ERROR 1 --- ...`.
_LINE_LEVEL = re.compile(r'\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\b')
_LEVELS = {'TRACE': logging.DEBUG, 'DEBUG': logging.DEBUG, 'INFO': logging.INFO,
           'WARN': logging.WARNING, 'WARNING': logging.WARNING, 'ERROR': logging.ERROR,
           'FATAL': logging.CRITICAL, 'CRITICAL': logging.CRITICAL}
# Pi's configuration directory for the container's `agent` user.
PI_CONTAINER_DIR = '/home/agent/.pi/agent'
# Subdirectory of PI_CREDENTIALS_DIR that every container gets as its Pi directory.
PI_SHARED_DIR = 'containers'
# Credentials Claude Code reads from its environment; a login kept in the host's
# keychain or ~/.claude cannot follow it into a container.
CLAUDE_CREDENTIALS = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN')
# These providers can also run in a separate sandbox daemon using environment
# credentials, without bind-mounting a worker-local Pi login into that daemon.
PI_API_KEYS = {'openai': 'OPENAI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY'}
# Where a stage keeps the workspace of an agent that paused for the user's answers.
PAUSED_DIR = 'paused'
MAX_STATE_BYTES = 16 * 1024 * 1024


class AgentCredentialsError(RuntimeError):
    """The model the stage's settings select has no credentials on this worker. Its message
    names what to set, never a secret, so it may be shown to the user."""


class AgentPaused(Exception):
    """The agent asked the user something and stopped; its workspace is kept until a new
    container continues from it with the answers."""

    def __init__(self, questions: list[Question]) -> None:
        super().__init__(f'Agent paused for answers to {len(questions)} questions')
        self.questions = questions


class AgentLimited(Exception):
    """The agent's provider refused its requests for its limits (rate, quota or usage); its
    workspace is kept, as a paused agent's is, until a new container continues from it
    once the provider is available."""

    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__(f'Agent stopped by its provider\'s limits: {report.get("message")}')
        self.report = report


# The ApplicationError type of a stage activity its worker stopped by shutting down (as for
# a redeploy): the stage's workflow runs it again, which goes on from what it saved.
INTERRUPTED_ERROR = 'StageInterrupted'


class AgentInterrupted(Exception):
    """The worker shut down while the agent ran. The agent was stopped; `kept` tells
    whether it saved where it was, its workspace then kept, as a paused agent's is, for a
    new container to continue from. Otherwise what it wrote is in the output."""

    def __init__(self, kept: bool) -> None:
        super().__init__('The agent was stopped by its worker shutting down'
                         + ('; it saved where it was' if kept else ''))
        self.kept = kept


def worker_shutting_down() -> bool:
    return activity.in_activity() and activity.is_worker_shutdown()


def has_saved_run(paused: Path) -> bool:
    """Whether `paused` holds a stopped agent's workspace that a new container can
    continue from."""
    return (paused / STATE_FILENAME).is_file()


def resume_workspace(paused: Path, workspace: Path, answers: list[str] | None) -> None:
    """Fill `workspace` with the one a paused agent left, and the answers it waits for;
    without `answers`, the one an agent stopped by its provider's limits left."""
    if not (paused / STATE_FILENAME).is_file():
        raise ApplicationError('The paused agent run is unavailable', non_retryable=True)
    shutil.copytree(paused, workspace, symlinks=True)
    if answers is not None:
        (workspace / ANSWERS_FILENAME).write_text(json.dumps({'answers': answers},
                                                             ensure_ascii=False))


def _read_small(path: Path, what: str) -> str:
    """A file the agent wrote, read as untrusted: regular and bounded."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_STATE_BYTES:
        raise RuntimeError(f'The agent stopped without saving its {what}')
    return path.read_text()


async def _keep_workspace(name: str, paused: Path, read: Any) -> Any:
    """Copy the stopped container's workspace to `paused`, once `read` accepted what the
    agent saved in it; returns what `read` returned."""
    staging = paused.parent / f'.{paused.name}-{uuid.uuid4().hex}'
    await run_sync(staging.mkdir, parents=True)
    try:
        await docker('cp', name + ':/workspace/.', str(staging))
        result = await run_sync(read, staging)
        if await run_sync(paused.exists):
            await run_sync(shutil.rmtree, paused)
        await run_sync(os.replace, staging, paused)
    except BaseException:
        await run_sync(shutil.rmtree, staging, ignore_errors=True)
        raise
    return result


def _paused_questions(staging: Path) -> list[Question]:
    try:
        pending = json.loads(_read_small(staging / STATE_FILENAME, 'state'))['pending']
        return normalize_questions(pending['questions'])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f'The agent paused with an unusable state: {exc}') from exc


def _limit_report(staging: Path) -> dict[str, Any]:
    _read_small(staging / STATE_FILENAME, 'state')
    try:
        report = json.loads(_read_small(staging / LIMIT_FILENAME, 'limit report'))
        wait = report.get('retry_after')
        return {'retry_after': float(wait) if isinstance(wait, (int, float)) else None,
                'message': str(report.get('message') or '')[:MESSAGE_CHARS]}
    except (ValueError, AttributeError, TypeError) as exc:
        raise RuntimeError(f'The agent stopped with an unusable limit report: {exc}') from exc


def _interrupted_state(staging: Path) -> None:
    try:
        state = json.loads(_read_small(staging / STATE_FILENAME, 'state'))
    except ValueError as exc:
        raise RuntimeError(f'The agent stopped with an unusable state: {exc}') from exc
    if not isinstance(state, dict) or state.get('pending') is not None:
        raise RuntimeError('The agent stopped with an unusable state')


async def _keep_paused(name: str, paused: Path) -> list[Question]:
    """Copy the paused container's workspace to `paused`; the questions it asked."""
    return await _keep_workspace(name, paused, _paused_questions)


@dataclass(frozen=True)
class AgentImageBuild:
    """Trusted build inputs; custom stages may select a different toolchain/context."""
    context: Path
    dockerfile: Path
    build_args: Mapping[str, str] = field(default_factory=dict)


_BACKEND = Path(__file__).resolve().parent.parent


def default_agent_image(role: str, workspace_name: str) -> AgentImageBuild:
    """The built-in agent image (Python 3.12, Java 21 and Maven), shared by the coding stages."""
    return AgentImageBuild(
        context=_BACKEND, dockerfile=_BACKEND / 'app/agent_workflows/Dockerfile',
        build_args={'AGENT_ROLE': role, 'WORKSPACE_NAME': workspace_name},
    )


MIN_SECRET_LENGTH = 8
# Secrets known to this worker beyond its environment, such as the saved OpenAI API key.
_KNOWN_SECRETS: set[str] = set()


def remember_secret(value: str) -> None:
    """Redact `value` from every output from now on."""
    if len(value) >= MIN_SECRET_LENGTH:
        _KNOWN_SECRETS.add(value)


def redact_output(output: str, env: Mapping[str, str] | None = None) -> str:
    """Redact known host/provider secrets before returning, saving or logging output.

    Values shorter than MIN_SECRET_LENGTH are left alone: settings such as
    AUTH_TOKEN_TTL_HOURS=12 match the names but are no secrets, and replacing them
    would corrupt ordinary output, such as a PID in `docker inspect` JSON."""
    return _redact(output, _secret_values(env))


def _secret_values(env: Mapping[str, str] | None) -> list[str]:
    secrets = set(_KNOWN_SECRETS)
    for key, value in (env if env is not None else os.environ).items():
        if (len(value) >= MIN_SECRET_LENGTH
                and any(part in key.upper() for part in ('KEY', 'TOKEN', 'PASSWORD', 'SECRET'))):
            secrets.add(value)
    return sorted(secrets, key=len, reverse=True)


def _redact(output: str, secrets: list[str]) -> str:
    for secret in secrets:
        output = output.replace(secret, '[redacted]')
    return output


def line_level(line: str, previous: int, default: int) -> int:
    """The level a container's output line is logged at: the level it names near its
    start; for an indented line, such as a stack frame, that of the line before it."""
    match = _LINE_LEVEL.search(line, 0, 64)
    if match:
        return _LEVELS[match.group(1)]
    return previous if line[:1].isspace() else default


async def _forward_stream(stream: asyncio.StreamReader, label: str, default: int,
                          secrets: list[str]) -> None:
    level = default

    def emit(raw: bytes) -> None:
        nonlocal level
        line = _redact(raw.decode('utf-8', 'replace').rstrip('\r'), secrets)
        if not line.strip():
            return
        level = line_level(line, level, default)
        container_logger.log(level, '[%s] %s', label, line[:CONTAINER_LINE_CHARS])

    pending = b''
    while chunk := await stream.read(65536):
        *lines, pending = (pending + chunk).split(b'\n')
        for line in lines:
            emit(line)
        if len(pending) > CONTAINER_LINE_CHARS * 4:
            emit(pending)
            pending = b''
    if pending:
        emit(pending)


async def _follow(name: str, label: str, secrets: list[str]) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            'docker', 'logs', '--follow', name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.warning('Could not follow the logs of container %s: %s', name, exc)
        return
    try:
        # Without a TTY, Docker keeps the container's stdout and stderr apart.
        assert process.stdout is not None and process.stderr is not None  # both piped
        await asyncio.gather(
            _forward_stream(process.stdout, label, logging.INFO, secrets),
            _forward_stream(process.stderr, label, logging.WARNING, secrets),
        )
        await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@asynccontextmanager
async def forward_logs(name: str, label: str, env: Mapping[str, str] | None = None):
    """Re-log a started container's output, as it is written, on `app.containers`: so it
    reaches the worker's own log, filtered by LOG_LEVEL. A line is logged at the level it
    names, else stdout at INFO and stderr at WARNING. Known secrets of `env` (default:
    the worker's environment) are redacted. Never fails the run it observes."""
    follower = asyncio.create_task(_follow(name, label, _secret_values(env)))

    async def stop() -> None:
        follower.cancel()
        await asyncio.gather(follower, return_exceptions=True)

    try:
        yield
    except BaseException:
        await stop()
        raise
    try:
        # The container has stopped or is removed next: let its last lines arrive.
        await asyncio.wait_for(asyncio.shield(follower), LOG_DRAIN_SECONDS)
    except TimeoutError:
        pass
    except Exception:
        logger.warning('Could not forward the logs of container %s', name, exc_info=True)
    finally:
        await stop()


STOP_GRACE_SECONDS = 10


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    """Stop a running process: SIGTERM, then SIGKILL if it is still running after
    STOP_GRACE_SECONDS; always wait until it has exited and been reaped, even when the
    calling task is cancelled again meanwhile (the cancellation is raised afterwards)."""

    async def stop() -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), STOP_GRACE_SECONDS)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    task = asyncio.ensure_future(stop())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                break
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    task.result()


async def docker(*args: str, timeout: int = 1800, env: dict | None = None,
                 stdin: Any = None, log_path: Path | None = None) -> str:
    # Never log argv/environment: -e and --build-arg may contain credentials.
    operation = ' '.join(args[:2]) if args[0] in {'image', 'network', 'container'} else args[0]
    command_id = uuid.uuid4().hex[:8]
    started = time.monotonic()
    logger.info('Docker [%s] %s started (timeout=%ss)', command_id, operation, timeout)
    process = None
    failure = None
    async with async_context(tempfile.TemporaryFile) as log:
        try:
            process = await asyncio.create_subprocess_exec(
                'docker', *args, stdout=log, stderr=asyncio.subprocess.STDOUT, env=env, stdin=stdin,
            )
            await asyncio.wait_for(process.wait(), timeout)
        except BaseException as exc:
            failure = exc
            if process is not None and process.returncode is None:
                await _stop_process(process)
            raise
        finally:
            def read_tail() -> str:
                log.seek(0, 2)
                log.seek(max(0, log.tell() - 8000))
                return log.read().decode('utf-8', 'replace').strip()

            output = redact_output(await run_sync(read_tail), env)
            elapsed = time.monotonic() - started
            exit_code = process.returncode if process is not None else None
            outcome = type(failure).__name__ if failure is not None else f'exit={exit_code}'
            failed = failure is not None or exit_code != 0
            emit = logger.error if failed else logger.info
            emit('Docker [%s] %s finished (%s, %.2fs)', command_id, operation, outcome, elapsed)
            if output:
                (logger.error if failed else logger.debug)(
                    'Docker [%s] %s output tail:\n%s', command_id, operation, output,
                )
            if log_path is not None:
                try:
                    await run_sync(log_path.parent.mkdir, parents=True, exist_ok=True)
                    await run_sync(
                        log_path.write_text,
                        f'Docker {operation}: {outcome}, {elapsed:.2f}s\n{output}\n',
                        encoding='utf-8',
                    )
                    logger.info('Docker [%s] output saved to %s', command_id, log_path)
                except OSError:
                    logger.warning('Could not save Docker [%s] log to %s', command_id, log_path)
        assert process is not None  # a command that did not start raised above
        if process.returncode:
            raise RuntimeError(f'Docker {args[0]} failed: {output}')
        return output


# How long one pull of a pinned image may take, how often it is tried, and how long the
# worker waits before trying it again.
PULL_SECONDS = 900
PULL_ATTEMPTS = 3
PULL_BACKOFF_SECONDS = (15, 60)
# The pull of each image in progress in this process, shared by every caller.
_pulls: dict[str, asyncio.Task] = {}


class ImageUnavailable(Exception):
    """A pinned image the worker needs could not be pulled: an infrastructure error of the
    stage, not a test failure or a contract mismatch, so it is no RuntimeError, which
    those are."""


async def _image_present(reference: str) -> bool:
    try:
        await docker('image', 'inspect', '--format', '{{.Id}}', reference, timeout=60)
    except RuntimeError:
        return False
    return True


async def _pull(reference: str) -> None:
    started = time.monotonic()
    logger.info('Pulling the image %s ahead of use', reference)
    failure: BaseException | None = None
    for attempt in range(1, PULL_ATTEMPTS + 1):
        try:
            await docker('pull', '--quiet', reference, timeout=PULL_SECONDS)
        except (RuntimeError, TimeoutError) as exc:
            failure = exc
            logger.warning('Pulling the image %s failed (attempt %d of %d): %s', reference,
                           attempt, PULL_ATTEMPTS, str(exc)[-500:])
            if attempt < PULL_ATTEMPTS:
                await asyncio.sleep(PULL_BACKOFF_SECONDS[min(attempt, len(PULL_BACKOFF_SECONDS)) - 1])
            continue
        logger.info('Pulled the image %s in %.1fs', reference, time.monotonic() - started)
        return
    logger.error('Could not pull the image %s in %d attempts (%.1fs)', reference,
                 PULL_ATTEMPTS, time.monotonic() - started)
    raise ImageUnavailable(
        f'The image {reference} is not on the worker and could not be pulled: '
        f'{str(failure)[-500:]}')


async def ensure_image(reference: str) -> None:
    """Make sure the Docker daemon holds `reference`: one inspect when it does, otherwise
    a pull, shared with any other caller in this process that waits for the same image
    (such as the pull the worker starts with). Raises ImageUnavailable, naming the image,
    when it cannot be pulled."""
    if await _image_present(reference):
        return
    task = _pulls.get(reference)
    if task is None or task.done():
        task = asyncio.create_task(_pull(reference), name=f'pull {reference}')
        _pulls[reference] = task
    # A caller that is cancelled leaves the pull to the others.
    await asyncio.shield(task)


async def ensure_images(references: tuple[str, ...] = PREPULLED_IMAGES) -> None:
    """Pull each image the worker will need that its Docker daemon lacks, one after the
    other, logging what failed: run in the background at the worker's start, so that
    nothing waits for it but a stage that needs the image."""
    for reference in references:
        started = time.monotonic()
        try:
            await ensure_image(reference)
        except ImageUnavailable as exc:
            logger.error('%s', exc)
        except Exception:
            logger.exception('Could not make sure the image %s is pulled', reference)
        else:
            logger.info('The image %s is on the worker (%.1fs)', reference,
                        time.monotonic() - started)


def container_base_url(base_url: str) -> str:
    """Reach a local provider from Docker while preserving remote provider URLs."""
    return _container_url(base_url)


def _container_url(base_url: str) -> str:
    url = urlsplit(base_url)
    if url.hostname in {'localhost', '127.0.0.1', '::1'}:
        authority = 'host.docker.internal' + (f':{url.port}' if url.port else '')
        return urlunsplit(url._replace(netloc=authority))
    return base_url


def pi_container_args(settings: ModelSettings) -> list[str]:
    """Supply the credentials required by a saved Pi adapter selection."""
    if settings.adapter != 'pi':
        return []
    credential = PI_API_KEYS.get(settings.model.partition('/')[0])
    if credential and os.getenv(credential) and not (PI_CREDENTIALS_DIR / 'models.json').is_file():
        # API keys need no worker-local files, even if Pi created an empty auth.json
        # while starting. Custom model configurations still use the file mounts below.
        # Forward the variable name so its value never enters Docker arguments.
        return ['-e', credential]
    if not (PI_CREDENTIALS_DIR / 'auth.json').is_file():
        raise AgentCredentialsError(
            f'Pi credentials not found: {PI_CREDENTIALS_DIR / "auth.json"}. '
            'Sign in with pi, set PI_CREDENTIALS_DIR, or configure the '
            'selected provider API key (OPENAI_API_KEY or ANTHROPIC_API_KEY).')
    if PI_CREDENTIALS_SHARED:
        # Coolify mounts this dedicated volume in the worker and daemon at the
        # same path. Sharing the directory (rather than a single auth.json bind)
        # preserves atomic credential replacements and a common refresh lock.
        return ['-v', f'{PI_CREDENTIALS_DIR}:{PI_CONTAINER_DIR}']
    # Pi serializes token refreshes with a lock directory created beside auth.json, so
    # the containers' Pi directory is one host directory: agents running in parallel then
    # contend for the same lock instead of each refreshing the token on its own.
    shared = PI_CREDENTIALS_DIR / PI_SHARED_DIR
    shared.mkdir(mode=0o700, exist_ok=True)
    # Single files, not the credentials directory: the agent must not reach the host's Pi
    # extensions or sessions. auth.json is writable so a refreshed token is not lost to
    # the host. Each file is mounted over a placeholder inside the shared directory.
    (shared / 'auth.json').touch(mode=0o600)
    args = ['-v', f'{shared}:{PI_CONTAINER_DIR}',
            '-v', f'{PI_CREDENTIALS_DIR / "auth.json"}:{PI_CONTAINER_DIR}/auth.json']
    if (PI_CREDENTIALS_DIR / 'models.json').is_file():
        (shared / 'models.json').touch(mode=0o600)
        args += ['-v', f'{PI_CREDENTIALS_DIR / "models.json"}:{PI_CONTAINER_DIR}/models.json:ro']
    else:
        (shared / 'models.json').unlink(missing_ok=True)
    return args


def claude_container_args(settings: ModelSettings) -> list[str]:
    """Forward the credentials required by a saved Claude adapter selection."""
    if settings.adapter != 'claude':
        return []
    credentials = [key for key in CLAUDE_CREDENTIALS if os.getenv(key)]
    if not credentials:
        raise AgentCredentialsError('Claude credentials not found: set ANTHROPIC_API_KEY, or '
                                    'CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`.')
    # Secrets are forwarded by name, so their values stay out of the docker arguments.
    args = [argument for key in credentials for argument in ('-e', key)]
    # Agent containers serve only the stages after the API design, whose turns follow
    # each other closely: Claude Code's 5-minute prompt cache suffices there, and it
    # overrides the 1-hour TTL a subscription login would otherwise get. Pi already
    # caches for 5 minutes unless PI_CACHE_RETENTION=long, which is not forwarded.
    args += ['-e', 'FORCE_PROMPT_CACHING_5M=1']
    if os.getenv('ANTHROPIC_BASE_URL'):
        args += ['-e', f'ANTHROPIC_BASE_URL={_container_url(os.environ["ANTHROPIC_BASE_URL"])}']
    return args


def check_openai_credentials(settings: ModelSettings, endpoint: OpenAIEndpoint | None) -> None:
    """The OpenAI-compatible adapter cannot start without the OpenAI endpoint saved in
    Settings, so a stage set to it fails here, saying why, rather than in its container."""
    if settings.adapter == 'openai' and endpoint is None:
        raise AgentCredentialsError(
            f'The selected model ({settings.model}) uses the openai adapter, but no OpenAI '
            'endpoint is set up in Settings.')


async def openai_environment(settings: ModelSettings) -> dict[str, str]:
    """The saved OpenAI endpoint as the container's OPENAI_API_KEY and OPENAI_BASE_URL,
    for a stage set to the openai adapter; nothing for any other."""
    if settings.adapter != 'openai':
        return {}
    endpoint = await get_openai_endpoint()
    check_openai_credentials(settings, endpoint)
    assert endpoint is not None
    remember_secret(endpoint.api_key)
    # A key is required by the client even where the endpoint needs none.
    return {'OPENAI_API_KEY': endpoint.api_key or NO_API_KEY,
            'OPENAI_BASE_URL': container_base_url(endpoint.base_url)}


@asynccontextmanager
async def keep_activity_alive(message: str):
    async def heartbeat() -> None:
        while True:
            activity.heartbeat(message)
            await asyncio.sleep(20)

    pulse = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def _container_name(role: str, session_id: str, kind: str | None = None) -> str:
    """`<role>[-<kind>]-<sha256 of the session ID>-<unique suffix>`: traceable to its
    session, and never shared by two runs."""
    parts = [role.replace('_', '-'), *([kind] if kind else []), _session_hash(session_id),
             uuid.uuid4().hex]
    return '-'.join(parts)


def _ignore_patterns(image_build: AgentImageBuild) -> tuple[bytes, list[tuple[bool, list[str]]]]:
    """The .dockerignore Docker applies to the build (`<Dockerfile>.dockerignore` beside
    the Dockerfile first, else the context's), as its bytes and its parsed patterns."""
    for candidate in (image_build.dockerfile.parent / (image_build.dockerfile.name + '.dockerignore'),
                      image_build.context / '.dockerignore'):
        try:
            content = candidate.read_bytes()
        except OSError:
            continue
        patterns: list[tuple[bool, list[str]]] = []
        for line in content.decode('utf-8', 'replace').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            negated = line.startswith('!')
            if negated:
                line = line[1:].strip()
            segments = [s for s in os.path.normpath(line).replace('\\', '/').split('/')
                        if s and s != '.']
            if segments:
                patterns.append((negated, segments))
        return content, patterns
    return b'', []


def _segments_match(pattern: list[str], path: list[str]) -> bool:
    if not pattern:
        return not path
    head = pattern[0]
    if head == '**':
        return any(_segments_match(pattern[1:], path[index:]) for index in range(len(path) + 1))
    return bool(path) and _segment_match(head, path[0]) and _segments_match(pattern[1:], path[1:])


def _segment_match(pattern: str, name: str) -> bool:
    return fnmatch.fnmatchcase(name, pattern)


def _ignored(patterns: list[tuple[bool, list[str]]], path: list[str]) -> bool:
    """Whether Docker leaves `path` out of the context: the last pattern matching it or
    one of its parent directories decides."""
    excluded = False
    for negated, pattern in patterns:
        if any(_segments_match(pattern, path[:length]) for length in range(1, len(path) + 1)):
            excluded = not negated
    return excluded


def _could_include_below(patterns: list[tuple[bool, list[str]]], directory: list[str]) -> bool:
    """Whether a negated pattern could take back a path inside excluded `directory`."""
    for negated, pattern in patterns:
        if not negated:
            continue
        for index, segment in enumerate(directory):
            if index >= len(pattern):
                break
            if pattern[index] == '**':
                return True
            if not _segment_match(pattern[index], segment):
                break
        else:
            return True
    return False


def _build_inputs_digest(image_build: AgentImageBuild) -> str:
    """A hash of an image's build inputs: the Dockerfile's contents, the contents of the
    files of the build context Docker sends and the build args."""
    digest = hashlib.sha256()

    def add(kind: bytes, *values: bytes) -> None:
        digest.update(kind)
        for value in values:
            digest.update(len(value).to_bytes(8, 'big'))
            digest.update(value)

    try:
        add(b'dockerfile', image_build.dockerfile.read_bytes())
    except OSError:
        add(b'no-dockerfile')
    for key, value in sorted(image_build.build_args.items()):
        add(b'arg', key.encode(), value.encode())
    ignore, patterns = _ignore_patterns(image_build)
    add(b'ignore', ignore)
    context = image_build.context
    for root, directories, files in os.walk(context, followlinks=False):
        relative_root = Path(root).relative_to(context).parts
        kept = []
        for directory in sorted(directories):
            path = [*relative_root, directory]
            full = Path(root) / directory
            if full.is_symlink():
                files.append(directory)
            elif not (_ignored(patterns, path) and not _could_include_below(patterns, path)):
                kept.append(directory)
        directories[:] = kept
        for name in sorted(files):
            path = [*relative_root, name]
            if _ignored(patterns, path):
                continue
            full = Path(root) / name
            relative = '/'.join(path).encode()
            try:
                if full.is_symlink():
                    add(b'link', relative, os.readlink(full).encode())
                elif full.is_file():
                    mode = b'x' if os.access(full, os.X_OK) else b'-'
                    file_digest = hashlib.sha256()
                    with full.open('rb') as stream:
                        while chunk := stream.read(1024 * 1024):
                            file_digest.update(chunk)
                    add(b'file', relative, mode, file_digest.digest())
            except OSError:
                # Gone while it was read: nothing Docker would send either.
                continue
    return digest.hexdigest()[:16]


async def _agent_image(role: str, image_build: AgentImageBuild) -> str:
    """The tag of the role's image: the role and a hash of its build inputs, shared by
    every session with the same inputs."""
    digest = await run_sync(_build_inputs_digest, image_build)
    return f"{role.replace('_', '-')}-agent:{digest}"


async def _image_exists(tag: str) -> bool:
    try:
        output = await docker('image', 'inspect', '--format', '{{.Id}}', tag, timeout=60)
    except Exception:
        return False
    return bool(output.strip())


async def _build_image(image_build: AgentImageBuild, tag: str, log_path: Path) -> None:
    """Build the image under `tag` unless it already exists locally."""
    if await _image_exists(tag):
        logger.info('Reusing agent image %s', tag)
        return
    build_args = ['build', '-t', tag, '-f', str(image_build.dockerfile)]
    for key, value in image_build.build_args.items():
        build_args.extend(['--build-arg', f'{key}={value}'])
    await docker(*build_args, str(image_build.context), log_path=log_path)


MAX_TOOL_REPORT_BYTES = 8 * 1024 * 1024


async def run_tool(role: str, session_id: str, workspace: Path, command: str, report: str,
                   log_path: Path, *, image_build: AgentImageBuild, workdir: str = 'review',
                   timeout: int = 600) -> str:
    """Run a trusted shell command in the role's agent image over a copy of `workspace`,
    in /workspace/<workdir>, without a network or any credential, and return the report
    it wrote to the file `report` (given to it as $REPORT)."""
    name = _container_name(role, session_id, 'tool')
    image = await _agent_image(role, image_build)
    await _build_image(image_build, image, log_path.parent / 'agent-build.log')
    try:
        await docker(
            'create', '--name', name, '--network=none', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--pids-limit=256', '--memory=2g',
            '--cpus=2', '--user', '10001:10001', '-e', f'REPORT={report}',
            '--workdir', f'/workspace/{workdir}', '--entrypoint', 'sh', image, '-c', command)
        async with async_context(tempfile.TemporaryDirectory) as temporary:
            archive = Path(temporary) / 'workspace.tar'
            await run_sync(_archive_workspace, workspace, archive)
            async with async_context(archive.open, 'rb') as stream:
                await docker('cp', '-a', '-', name + ':/workspace', stdin=stream)
            await docker('start', name)
            exit_code = (await docker('wait', name, timeout=timeout)).strip().splitlines()[-1]
            if exit_code != '0':
                log = redact_output(await docker('logs', '--tail', '50', name, timeout=30))
                await run_sync(log_path.write_text, log, encoding='utf-8')
                raise RuntimeError(f'The tool exited with code {exit_code}. Log: {log_path}')
            copied = Path(temporary) / 'report'
            await docker('cp', f'{name}:{report}', str(copied))
            if copied.is_symlink() or copied.stat().st_size > MAX_TOOL_REPORT_BYTES:
                raise RuntimeError('The tool\'s report is missing or too large')
            return await run_sync(copied.read_text, encoding='utf-8', errors='replace')
    finally:
        try:
            await docker('rm', '-f', name, timeout=30)
        except Exception:
            logger.warning('Could not remove tool container %s', name)


CHECKS_LOG_LINES = 20000
# At most this much of the end of a script's output is kept, however long its lines.
CHECKS_LOG_BYTES = 4 * 1024 * 1024


async def run_script(role: str, session_id: str, workspace: Path, command: str,
                     log_path: Path, *, image_build: AgentImageBuild, workdir: str,
                     timeout: int) -> int | None:
    """Run a trusted shell command over a copy of `workspace` in the role's agent image,
    in /workspace/<workdir>, with a network (to install dependencies) but no credential, no
    host mount and no Docker. Its output, redacted, goes to `log_path`; returns its exit
    status, or None when it ran out of `timeout` seconds."""
    name = _container_name(role, session_id, 'script')
    image = await _agent_image(role, image_build)
    await _build_image(image_build, image, log_path.parent / 'agent-build.log')
    try:
        await docker(
            'create', '--name', name, '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--pids-limit=1024', '--memory=6g', '--cpus=2', '--user', '10001:10001',
            '-e', 'HOME=/home/agent', '-e', 'CI=true', '-e', 'PYTHONDONTWRITEBYTECODE=1',
            '--workdir', f'/workspace/{workdir}', '--entrypoint', 'sh', image, '-c', command)
        async with async_context(tempfile.TemporaryDirectory) as temporary:
            archive = Path(temporary) / 'workspace.tar'
            await run_sync(_archive_workspace, workspace, archive)
            async with async_context(archive.open, 'rb') as stream:
                await docker('cp', '-a', '-', name + ':/workspace', stdin=stream)
        await docker('start', name)
        exit_code: int | None
        try:
            exit_code = int((await docker('wait', name, timeout=timeout)).strip().splitlines()[-1])
        except TimeoutError:
            exit_code = None
            await docker('kill', name, timeout=30)
        await _save_logs(name, log_path)
        return exit_code
    finally:
        try:
            await docker('rm', '-f', name, timeout=30)
        except Exception:
            logger.warning('Could not remove script container %s', name)


async def _save_logs(name: str, log_path: Path) -> None:
    """Write the container's whole output, its last CHECKS_LOG_LINES lines, redacted, to
    `log_path`: docker() keeps only the end of what a command prints."""
    process = await asyncio.create_subprocess_exec(
        'docker', 'logs', '--tail', str(CHECKS_LOG_LINES), name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output = bytearray()
    cut = False
    try:
        async with asyncio.timeout(120):
            # Read as it arrives, keeping only the end: the output is the repository's
            # script's, as large as it likes.
            assert process.stdout is not None  # piped
            while chunk := await process.stdout.read(65536):
                output.extend(chunk)
                if len(output) > CHECKS_LOG_BYTES:
                    del output[:len(output) - CHECKS_LOG_BYTES]
                    cut = True
            await process.wait()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    text = redact_output(output.decode('utf-8', 'replace'))
    if cut:
        text = '… (the start of the output was cut)\n' + text.split('\n', 1)[-1]
    await run_sync(log_path.write_text, text, encoding='utf-8')


async def _salvage_output(name: str, output: Path, output_name: str) -> None:
    """Copy a failed agent's project out, if it can be; a failure here is only logged."""
    try:
        await run_sync(output.mkdir, exist_ok=True)
        await docker('cp', name + f':/workspace/{output_name}/.', str(output), timeout=300)
    except Exception as exc:
        logger.warning('Could not copy the failed agent\'s project out of %s: %s', name, exc)
        await run_sync(shutil.rmtree, output, ignore_errors=True)


def _archive_workspace(workspace: Path, archive: Path) -> None:
    def owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 10001
        info.uname = info.gname = 'agent'
        return info

    with tarfile.open(archive, 'w') as tar:
        for item in workspace.iterdir():
            tar.add(item, arcname=item.name, filter=owner)


async def _stop_agent(name: str, role: str, session_id: str, reason: str) -> None:
    """Ask an agent that has to end early to stop, so that it exports its traces before
    it is removed; `docker rm -f` alone kills it with whatever it had not exported."""
    logger.warning('%s agent %s; stopping it (session=%s, container=%s)',
                   role, reason, session_id, name)
    try:
        await docker('stop', '-t', str(AGENT_STOP_GRACE_SECONDS), name,
                     timeout=AGENT_STOP_GRACE_SECONDS + 30)
    except Exception:
        logger.warning('Could not stop agent container %s', name, exc_info=True)


async def run_agent(role: str, session_id: str, workspace: Path,
                    output: Path, output_name: str, log_path: Path, *,
                    image_build: AgentImageBuild, paused: Path) -> None:
    """Copy only prepared inputs in and a single output tree out. The only host mounts are
    the Pi credential files and their shared lock directory, when the Pi harness is
    selected. An agent that asks the user something raises AgentPaused once its whole
    workspace is kept in `paused`; any other end discards what was kept there."""
    label = role.replace('_', '-')
    name = _container_name(role, session_id)
    agent_image = await _agent_image(role, image_build)
    settings = await get_workflow_settings(cast(WorkflowType, role))
    # The snapshot travels as input alongside the workspace, never as host selector env vars.
    await run_sync((workspace / SETTINGS_FILENAME).write_text, settings.model_dump_json())
    try:
        openai = await openai_environment(settings)
        adapter_args = [*(await run_sync(pi_container_args, settings)),
                        *claude_container_args(settings)]
    except AgentCredentialsError as exc:
        label_text = WORKFLOW_LABELS.get(role, role.replace('_', ' ').capitalize())
        raise AgentCredentialsError(
            f'{exc} Set it, or choose another model for the {label_text} stage in '
            'Settings, then try again.') from exc
    trace_environment = container_trace_environment(session_id, role)
    # A self-hosted loopback Langfuse endpoint also needs Docker's host alias.
    if 'LANGFUSE_BASE_URL' in trace_environment:
        trace_environment['LANGFUSE_BASE_URL'] = _container_url(trace_environment['LANGFUSE_BASE_URL'])
    environment = {**os.environ, **trace_environment, **openai}
    # Forwarded by name, so the saved key never enters Docker arguments.
    openai_args = [argument for key in openai for argument in ('-e', key)]
    trace_args = [argument for key in trace_environment for argument in ('-e', key)]
    # Runtime tuning is forwarded by name; nothing else of the host environment is.
    setting_args = [argument for key in AGENT_SETTINGS if key in os.environ
                    for argument in ('-e', key)]
    logger.info('%s agent starting (session=%s, container=%s)', role, session_id, name)
    kept = interrupted = False
    # Whether this run continues the one kept in `paused`, whose copy the workspace holds.
    continuing = await run_sync((workspace / STATE_FILENAME).is_file)
    try:
        await _build_image(image_build, agent_image, log_path.parent / 'agent-build.log')
        await docker(
            'create', '--name', name, '--cap-drop=ALL',
            # Threads count as processes: the CLI and a Maven build's JVMs need room. No
            # core dumps: a crashed CLI left one of gigabytes in the project it salvages.
            '--security-opt=no-new-privileges', '--pids-limit=1024', '--ulimit', 'core=0',
            '--memory=4g', '--cpus=2', '--user', '10001:10001',
            # No bytecode caches in the project: they were copied into what tests read as
            # text, and failed them in the sandbox only.
            '-e', 'PYTHONDONTWRITEBYTECODE=1',
            *openai_args, *trace_args, *setting_args, *adapter_args, agent_image, env=environment,
        )
        archive = workspace.parent / 'workspace.tar'

        await run_sync(_archive_workspace, workspace, archive)
        async with async_context(archive.open, 'rb') as stream:
            await docker('cp', '-a', '-', name + ':/workspace', stdin=stream)
        await docker('start', name)
        logger.info('%s agent running (session=%s, container=%s)', role, session_id, name)
        async with forward_logs(name, f'{label} agent {session_id}', environment):
            try:
                await docker('wait', name, timeout=AGENT_TIMEOUT_SECONDS)
            except TimeoutError:
                await _stop_agent(name, role, session_id, 'ran out of time')
                raise
            except asyncio.CancelledError:
                # Cancelled with its activity: stopped, the agent exports its observations
                # under the stage's span, which `docker rm -f` below would lose.
                await _stop_agent(name, role, session_id, 'was cancelled')
                if not worker_shutting_down():
                    raise
                # The worker shuts down, as for a redeploy: the agent saved where it was,
                # which is kept for the stage's next attempt, on the next worker.
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                interrupted = True
        state = json.loads(await docker('inspect', '--format', '{{json .State}}', name))
        if interrupted:
            if state['ExitCode'] == INTERRUPTED_EXIT:
                try:
                    await _keep_workspace(name, paused, _interrupted_state)
                    kept = True
                except Exception as exc:
                    logger.warning('Could not keep the stopped %s agent\'s workspace: %s',
                                   role, exc)
            if not kept:
                await _salvage_output(name, output, output_name)
            raise AgentInterrupted(kept)
        try:
            agent_log = await docker('logs', '--tail', '200', name, timeout=30)
        except Exception as exc:
            agent_log = f'Could not retrieve agent logs: {exc}'
        agent_log = redact_output(agent_log, environment)
        await run_sync(log_path.write_text, agent_log, encoding='utf-8')
        logger.info('%s agent finished (session=%s, exit=%s, log=%s)',
                    role, session_id, state['ExitCode'], log_path)
        if state['ExitCode'] == PAUSE_EXIT:
            questions = await _keep_paused(name, paused)
            kept = True
            raise AgentPaused(questions)
        if state['ExitCode'] == PROVIDER_LIMIT_EXIT:
            report = await _keep_workspace(name, paused, _limit_report)
            kept = True
            raise AgentLimited(report)
        if state['ExitCode'] != 0:
            # What the agent wrote before it failed is kept for the stage to save, as the
            # repository's work in progress; nothing of it is published.
            await _salvage_output(name, output, output_name)
            detail = ' (OOM killed)' if state.get('OOMKilled') else ''
            raise RuntimeError(
                f"{label.capitalize()} agent exited with code {state['ExitCode']}{detail}. "
                f"Log: {log_path}\n{agent_log[-4000:]}"
            )
        await run_sync(output.mkdir)
        await docker('cp', name + f':/workspace/{output_name}/.', str(output))
    except (AgentPaused, AgentLimited, AgentInterrupted) as exc:
        logger.info('%s agent paused (session=%s): %s', role, session_id, exc)
        raise
    except Exception:
        logger.exception('%s agent failed (session=%s, container=%s)', role, session_id, name)
        raise
    finally:
        # A continuing run its worker stopped before the agent saved anew keeps the
        # workspace it continued (with its questions, say), for the next attempt.
        if not kept and not (continuing and worker_shutting_down()):
            await run_sync(shutil.rmtree, paused, ignore_errors=True)
        try:
            await docker('rm', '-f', name, timeout=30)
        except Exception:
            activity.logger.warning('Could not remove agent container %s', name)
