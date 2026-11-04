"""Host-side Docker lifecycle for isolated coding agents; never mounted in agents."""

import asyncio
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
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.asyncio_utils import async_context, run_sync
from app.agent_workflows.runtime import ANSWERS_FILENAME, PAUSE_EXIT, STATE_FILENAME
from app.config import OPENAI_API_KEY, OPENAI_BASE_URL, PI_CREDENTIALS_DIR, PI_CREDENTIALS_SHARED
from app.llm.questions import Question, normalize_questions
from app.settings.models import ModelSettings, SETTINGS_FILENAME, WORKFLOW_LABELS
from app.settings.storage import get_workflow_settings
from app.tracing import container_trace_environment

logger = logging.getLogger(__name__)
# Output of sandboxed containers, re-logged line by line at each line's own level.
container_logger = logging.getLogger('app.containers')
# Forwarded by name; LOG_LEVEL sets the agent's own logging, which forward_logs re-logs.
# The *_MAX_TURNS budgets are read inside the container by each stage's AgentProfile.
AGENT_SETTINGS = ('AGENT_CONTEXT_TOKENS', 'LOG_LEVEL', 'IMPLEMENTATION_MAX_TURNS',
                  'TEST_GENERATION_MAX_TURNS', 'REVIEW_MAX_TURNS', 'DIAGNOSIS_MAX_TURNS',
                  'DOCUMENTATION_MAX_TURNS', 'ROUTING_MAX_TURNS')
CONTAINER_LINE_CHARS = 4000
# How long a container's remaining output may take to arrive once it was waited for.
LOG_DRAIN_SECONDS = 2
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


def resume_workspace(paused: Path, workspace: Path, answers: list[str]) -> None:
    """Fill `workspace` with the one a paused agent left, and the answers it waits for."""
    if not (paused / STATE_FILENAME).is_file():
        raise ApplicationError('The paused agent run is unavailable', non_retryable=True)
    shutil.copytree(paused, workspace, symlinks=True)
    (workspace / ANSWERS_FILENAME).write_text(json.dumps({'answers': answers},
                                                         ensure_ascii=False))


async def _keep_paused(name: str, paused: Path) -> list[Question]:
    """Copy the paused container's workspace to `paused`; the questions it asked."""
    staging = paused.parent / f'.{paused.name}-{uuid.uuid4().hex}'
    await run_sync(staging.mkdir, parents=True)
    try:
        await docker('cp', name + ':/workspace/.', str(staging))
        # The state is the agent's own output and is read as untrusted.
        state = staging / STATE_FILENAME
        if (
            await run_sync(state.is_symlink)
            or not await run_sync(state.is_file)
            or (await run_sync(state.stat)).st_size > MAX_STATE_BYTES
        ):
            raise RuntimeError('The agent paused without saving its state')
        try:
            pending = json.loads(await run_sync(state.read_text))['pending']
            questions = normalize_questions(pending['questions'])
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f'The agent paused with an unusable state: {exc}') from exc
        if await run_sync(paused.exists):
            await run_sync(shutil.rmtree, paused)
        await run_sync(os.replace, staging, paused)
    except BaseException:
        await run_sync(shutil.rmtree, staging, ignore_errors=True)
        raise
    return questions


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


def redact_output(output: str, env: Mapping[str, str] | None = None) -> str:
    """Redact known host/provider secrets before returning, saving or logging output.

    Values shorter than MIN_SECRET_LENGTH are left alone: settings such as
    AGENT_CONTEXT_TOKENS=46000 match the names but are no secrets, and replacing them
    would corrupt ordinary output, such as a PID in `docker inspect` JSON."""
    return _redact(output, _secret_values(env))


def _secret_values(env: Mapping[str, str] | None) -> list[str]:
    secrets = {OPENAI_API_KEY} if OPENAI_API_KEY else set()
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
                process.kill()
                await process.wait()
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
        if process.returncode:
            raise RuntimeError(f'Docker {args[0]} failed: {output}')
        return output


def container_base_url() -> str:
    """Reach a local provider from Docker while preserving remote provider URLs."""
    return _container_url(OPENAI_BASE_URL)


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


def check_openai_credentials(settings: ModelSettings) -> None:
    """The OpenAI-compatible adapter cannot even start without an API key, local servers
    included, so a stage set to it fails here, saying why, rather than in its container."""
    if settings.adapter == 'openai' and not OPENAI_API_KEY:
        raise AgentCredentialsError(
            f'The selected model ({settings.model}) uses the OpenAI-compatible adapter, but '
            'OPENAI_API_KEY is not set on the worker (any value works for a local server).')


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


def _archive_workspace(workspace: Path, archive: Path) -> None:
    def owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 10001
        info.uname = info.gname = 'agent'
        return info

    with tarfile.open(archive, 'w') as tar:
        for item in workspace.iterdir():
            tar.add(item, arcname=item.name, filter=owner)


async def run_agent(role: str, session_id: str, workspace: Path,
                    output: Path, output_name: str, log_path: Path, *,
                    image_build: AgentImageBuild, paused: Path) -> None:
    """Copy only prepared inputs in and a single output tree out. The only host mounts are
    the Pi credential files and their shared lock directory, when the Pi harness is
    selected. An agent that asks the user something raises AgentPaused once its whole
    workspace is kept in `paused`; any other end discards what was kept there."""
    label = role.replace('_', '-')
    name = f'{label}-{uuid.uuid4().hex}'
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    agent_image = f'temporal-{label}-agent:{digest}'
    settings = await get_workflow_settings(role)
    # The snapshot travels as input alongside the workspace, never as host selector env vars.
    await run_sync((workspace / SETTINGS_FILENAME).write_text, settings.model_dump_json())
    try:
        check_openai_credentials(settings)
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
    environment = {**os.environ, **trace_environment, 'OPENAI_API_KEY': OPENAI_API_KEY,
                   'OPENAI_BASE_URL': container_base_url()}
    trace_args = [argument for key in trace_environment for argument in ('-e', key)]
    # Runtime tuning is forwarded by name; nothing else of the host environment is.
    setting_args = [argument for key in AGENT_SETTINGS if key in os.environ
                    for argument in ('-e', key)]
    logger.info('%s agent starting (session=%s, container=%s)', role, session_id, name)
    kept = False
    try:
        build_args = ['build', '-t', agent_image, '-f', str(image_build.dockerfile)]
        for key, value in image_build.build_args.items():
            build_args.extend(['--build-arg', f'{key}={value}'])
        await docker(*build_args, str(image_build.context),
                     log_path=log_path.parent / 'agent-build.log')
        await docker(
            'create', '--name', name, '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--pids-limit=256',
            '--memory=4g', '--cpus=2', '--user', '10001:10001',
            '-e', 'OPENAI_API_KEY', '-e', 'OPENAI_BASE_URL',
            *trace_args, *setting_args, *adapter_args, agent_image, env=environment,
        )
        archive = workspace.parent / 'workspace.tar'

        await run_sync(_archive_workspace, workspace, archive)
        async with async_context(archive.open, 'rb') as stream:
            await docker('cp', '-a', '-', name + ':/workspace', stdin=stream)
        await docker('start', name)
        logger.info('%s agent running (session=%s, container=%s)', role, session_id, name)
        async with forward_logs(name, f'{label} agent {session_id}', environment):
            await docker('wait', name, timeout=3600)
        state = json.loads(await docker('inspect', '--format', '{{json .State}}', name))
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
        if state['ExitCode'] != 0:
            detail = ' (OOM killed)' if state.get('OOMKilled') else ''
            raise RuntimeError(
                f"{label.capitalize()} agent exited with code {state['ExitCode']}{detail}. "
                f"Log: {log_path}\n{agent_log[-4000:]}"
            )
        await run_sync(output.mkdir)
        await docker('cp', name + f':/workspace/{output_name}/.', str(output))
    except AgentPaused as exc:
        logger.info('%s agent paused (session=%s): %s', role, session_id, exc)
        raise
    except Exception:
        logger.exception('%s agent failed (session=%s, container=%s)', role, session_id, name)
        raise
    finally:
        if not kept:
            await run_sync(shutil.rmtree, paused, ignore_errors=True)
        try:
            await docker('rm', '-f', name, timeout=30)
        except Exception:
            activity.logger.warning('Could not remove agent container %s', name)
