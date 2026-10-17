"""Host-side Docker lifecycle for isolated coding agents; never mounted in agents."""

import asyncio
import hashlib
import json
import logging
import os
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

from app.agent_workflows.runtime import ANSWERS_FILENAME, PAUSE_EXIT, STATE_FILENAME
from app.config import OPENAI_API_KEY, OPENAI_BASE_URL, PI_CREDENTIALS_DIR
from app.llm.questions import Question, normalize_questions
from app.settings.models import ModelSettings, SETTINGS_FILENAME
from app.settings.storage import get_workflow_settings
from app.tracing import container_trace_environment

logger = logging.getLogger(__name__)
AGENT_SETTINGS = ('AGENT_CONTEXT_TOKENS',)
# Pi's configuration directory for the container's `agent` user.
PI_CONTAINER_DIR = '/home/agent/.pi/agent'
# Subdirectory of PI_CREDENTIALS_DIR that every container gets as its Pi directory.
PI_SHARED_DIR = 'containers'
# Credentials Claude Code reads from its environment; a login kept in the host's
# keychain or ~/.claude cannot follow it into a container.
CLAUDE_CREDENTIALS = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN')
# Where a stage keeps the workspace of an agent that paused for the user's answers.
PAUSED_DIR = 'paused'
MAX_STATE_BYTES = 16 * 1024 * 1024


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
    staging.mkdir(parents=True)
    try:
        await docker('cp', name + ':/workspace/.', str(staging))
        # The state is the agent's own output and is read as untrusted.
        state = staging / STATE_FILENAME
        if state.is_symlink() or not state.is_file() or state.stat().st_size > MAX_STATE_BYTES:
            raise RuntimeError('The agent paused without saving its state')
        try:
            pending = json.loads(state.read_text())['pending']
            questions = normalize_questions(pending['questions'])
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f'The agent paused with an unusable state: {exc}') from exc
        if paused.exists():
            shutil.rmtree(paused)
        os.replace(staging, paused)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return questions


@dataclass(frozen=True)
class AgentImageBuild:
    """Trusted build inputs; custom stages may select a different toolchain/context."""
    context: Path
    dockerfile: Path
    build_args: Mapping[str, str] = field(default_factory=dict)


def default_agent_image(role: str, workspace_name: str) -> AgentImageBuild:
    """The built-in agent image (Python 3.12, Java 21 and Maven), shared by the coding stages."""
    backend = Path(__file__).resolve().parent.parent
    return AgentImageBuild(
        context=backend, dockerfile=backend / 'app/agent_workflows/Dockerfile',
        build_args={'AGENT_ROLE': role, 'WORKSPACE_NAME': workspace_name},
    )


MIN_SECRET_LENGTH = 8


def redact_output(output: str, env: Mapping[str, str] | None = None) -> str:
    """Redact known host/provider secrets before returning, saving or logging output.

    Values shorter than MIN_SECRET_LENGTH are left alone: settings such as
    AGENT_CONTEXT_TOKENS=46000 match the names but are no secrets, and replacing them
    would corrupt ordinary output, such as a PID in `docker inspect` JSON."""
    secrets = {OPENAI_API_KEY} if OPENAI_API_KEY else set()
    for key, value in (env if env is not None else os.environ).items():
        if (len(value) >= MIN_SECRET_LENGTH
                and any(part in key.upper() for part in ('KEY', 'TOKEN', 'PASSWORD', 'SECRET'))):
            secrets.add(value)
    for secret in sorted(secrets, key=len, reverse=True):
        output = output.replace(secret, '[redacted]')
    return output


async def docker(*args: str, timeout: int = 1800, env: dict | None = None,
                 stdin: Any = None, log_path: Path | None = None) -> str:
    # Never log argv/environment: -e and --build-arg may contain credentials.
    operation = ' '.join(args[:2]) if args[0] in {'image', 'network', 'container'} else args[0]
    command_id = uuid.uuid4().hex[:8]
    started = time.monotonic()
    logger.info('Docker [%s] %s started (timeout=%ss)', command_id, operation, timeout)
    process = None
    failure = None
    with tempfile.TemporaryFile() as log:
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
            log.seek(0, 2)
            log.seek(max(0, log.tell() - 8000))
            output = redact_output(log.read().decode('utf-8', 'replace').strip(), env)
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
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text(
                        f'Docker {operation}: {outcome}, {elapsed:.2f}s\n{output}\n', encoding='utf-8',
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
    """Mount the credentials required by a saved Pi adapter selection."""
    if settings.adapter != 'pi':
        return []
    if not (PI_CREDENTIALS_DIR / 'auth.json').is_file():
        raise RuntimeError(f'Pi credentials not found: {PI_CREDENTIALS_DIR / "auth.json"}. '
                           'Sign in with pi or set PI_CREDENTIALS_DIR.')
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
        raise RuntimeError('Claude credentials not found: set ANTHROPIC_API_KEY, or '
                           'CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`.')
    # Secrets are forwarded by name, so their values stay out of the docker arguments.
    args = [argument for key in credentials for argument in ('-e', key)]
    if os.getenv('ANTHROPIC_BASE_URL'):
        args += ['-e', f'ANTHROPIC_BASE_URL={_container_url(os.environ["ANTHROPIC_BASE_URL"])}']
    return args


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
    (workspace / SETTINGS_FILENAME).write_text(settings.model_dump_json())
    adapter_args = [*pi_container_args(settings), *claude_container_args(settings)]
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

        def owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
            info.uid = info.gid = 10001
            info.uname = info.gname = 'agent'
            return info

        with tarfile.open(archive, 'w') as tar:
            for item in workspace.iterdir():
                tar.add(item, arcname=item.name, filter=owner)
        with archive.open('rb') as stream:
            await docker('cp', '-a', '-', name + ':/workspace', stdin=stream)
        await docker('start', name)
        logger.info('%s agent running (session=%s, container=%s)', role, session_id, name)
        await docker('wait', name, timeout=3600)
        state = json.loads(await docker('inspect', '--format', '{{json .State}}', name))
        try:
            agent_log = await docker('logs', '--tail', '200', name, timeout=30)
        except Exception as exc:
            agent_log = f'Could not retrieve agent logs: {exc}'
        agent_log = redact_output(agent_log, environment)
        log_path.write_text(agent_log, encoding='utf-8')
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
        output.mkdir()
        await docker('cp', name + f':/workspace/{output_name}/.', str(output))
    except AgentPaused as exc:
        logger.info('%s agent paused (session=%s): %s', role, session_id, exc)
        raise
    except Exception:
        logger.exception('%s agent failed (session=%s, container=%s)', role, session_id, name)
        raise
    finally:
        if not kept:
            shutil.rmtree(paused, ignore_errors=True)
        try:
            await docker('rm', '-f', name, timeout=30)
        except Exception:
            activity.logger.warning('Could not remove agent container %s', name)
