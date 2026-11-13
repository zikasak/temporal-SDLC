"""Host-only prepare/run/build/publish lifecycle with explicit stage policies."""
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from temporalio.exceptions import ApplicationError

from app import docker_agents
from app.asyncio_utils import async_context, run_sync
from app.languages import Language, UnsupportedLanguage, get_language
from app.tracing import trace_operation
from app.temporal.tracing import activity_metadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StageInputs:
    context: dict[str, Any]
    populate: Callable[[Path], None]


@dataclass(frozen=True)
class WorkTarget:
    """Where a stage's output belongs in the repository, for its work refs: the files
    `include` selects of it, placed at `prefix`."""
    stage: str
    include: Callable[[Path], bool]
    prefix: str = ''


@dataclass(frozen=True)
class StageSpec:
    role: str
    artifact_subdir: str
    workspace_name: str
    output_subdir: str
    heartbeat: str
    # What the image built from the output is called; None for an output that is
    # published as files only.
    image_label: str | None
    image_build: docker_agents.AgentImageBuild
    prepare_inputs: Callable[[Path, dict[str, Any]], StageInputs]
    sanitize_output: Callable[[Path, Path], None]
    validate_output: Callable[[Path], None]
    # Reads, from the agent's raw output, a result that takes the place of the stage's
    # output, such as a test author's finding that nothing it could write can test a
    # criterion. None goes on to sanitize, validate and publish the output.
    declined_result: Callable[[Path], dict[str, Any] | None] | None = None
    # Builds the image of the sanitized output (given with the build log's path) and
    # returns its ID, in place of building the output as it is: for a runner that needs
    # more than the output, such as the code its tests run in-process.
    build_output: Callable[[Path, Path], Awaitable[str]] | None = None
    # Where the output belongs in the session's repository, given the stage's context:
    # every run's output is saved to the repository's work refs (app.github.work_refs),
    # and a published output missing on this worker is restored from them. None for a
    # stage that changes nothing of the project.
    work: Callable[[dict[str, Any]], WorkTarget | None] | None = None


# The language's authoring instructions, placed beside the agent's project.
LANGUAGE_FILENAME = 'language.json'
# Context entry with the user's answers, which continues the stage's paused agent.
ANSWERS_KEY = 'answers'
# Context entry that continues the stage's agent that its provider's limits stopped.
RESUME_KEY = 'resume_after_limit'


def session_language(name: str | None) -> Language:
    """The registered language of a session, or a failure no retry can fix."""
    try:
        return get_language(name)
    except UnsupportedLanguage as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc


def write_language(workspace: Path, language: Language, **details: str) -> None:
    """Tell the container's agent what the session's language requires of it."""
    (workspace / LANGUAGE_FILENAME).write_text(json.dumps({
        'name': language.name, 'build_dirs': list(language.build_dirs), **details}))


def copy_file(source: Path, destination: Path) -> None:
    """Copy contents and whether the file is executable (a build helper such as mvnw
    must stay runnable), but no other permission or ownership bits."""
    shutil.copyfile(source, destination)
    destination.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)


def copy_regular_files(source: Path, target: Path, *,
                       include: Callable[[Path], bool]) -> None:
    """Copy selected regular files, never symlinks or files under linked directories."""
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob('*'):
        relative = item.relative_to(source)
        if not include(relative):
            continue
        if item.is_symlink() or any(parent.is_symlink() for parent in item.parents):
            continue
        if item.is_file():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_file(item, destination)


async def build_image(context: Path, label: str, log_path: Path,
                      build_args: dict[str, str] | None = None) -> str:
    """Build the image of a sanitized source tree and return its immutable ID."""
    iid = context.parent / 'image-id'
    arguments = [argument for key, value in (build_args or {}).items()
                 for argument in ('--build-arg', f'{key}={value}')]
    await docker_agents.docker('build', *arguments, '--iidfile', str(iid), str(context),
                               log_path=log_path)
    image_id = (await run_sync(iid.read_text)).strip()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
        raise RuntimeError(f'Docker returned an invalid {label} image ID')
    return image_id


def publish_manifest(artifact_dir: Path, result: dict[str, Any]) -> None:
    """Replace the stage's image.json in one step, so a reader never sees half of it."""
    manifest = artifact_dir / f'{uuid.uuid4().hex}.json'
    manifest.write_text(json.dumps(result, indent=2))
    os.replace(manifest, artifact_dir / 'image.json')


def paused_result(session_id: str, paused: docker_agents.AgentPaused) -> dict[str, Any]:
    """What a stage returns when its agent asked the user something."""
    return {'session_id': session_id, 'questions': paused.questions}


def limited_result(session_id: str, limited: docker_agents.AgentLimited) -> dict[str, Any]:
    """What a stage returns when its agent's provider refused it for its limits: the stage
    workflow waits, then continues the agent with RESUME_KEY in the context."""
    return {'session_id': session_id, 'provider_limit': limited.report}


async def _work_token() -> tuple[str, str]:
    """The GitHub token, and the commit author, of the work refs."""
    from app.temporal.activities import _github_token
    token, connection = await _github_token()
    bot = f"{connection['app_slug']}[bot]"
    return token, f"{bot}\n{connection['bot_user_id']}+{bot}@users.noreply.github.com"


WORK_MESSAGE_PROMPT = """Write the Git commit message of a work-in-progress commit of a
code-generating pipeline's {stage} stage ({status}), from its diff below. The first line is
the subject: at most 72 characters, imperative mood, naming what changed. Then an empty
line and at most five short lines on what changed and why. Plain text only, nothing else.

{diff}"""
WORK_MESSAGE_SECONDS = 60


async def _work_message(role: str, stage: str, status: str, diff: str) -> str | None:
    """The LLM's commit message of the stage's work; None when it could not write one in
    time. Never waits for a provider's limits: the fallback message is used instead."""
    from langchain_core.messages import HumanMessage
    from app.llm.graph import native_session
    from app.llm.model import get_model
    try:
        model = await get_model(role)
    except Exception:
        model = await get_model('review')
    async with asyncio.timeout(WORK_MESSAGE_SECONDS):
        async with native_session(model):
            reply = await model.ainvoke([HumanMessage(content=WORK_MESSAGE_PROMPT.format(
                stage=stage, status=status, diff=diff))])
    return str(getattr(reply, 'text', None) or getattr(reply, 'content', '') or '') or None


async def save_stage_work(stage: StageSpec, session_dir: Path, context: dict[str, Any],
                          tree: Path, status: str, published: bool = False) -> None:
    """Save `tree`, the stage's output, to the session repository's work refs; never fails."""
    from app.github import work_refs
    try:
        target = stage.work(context) if stage.work else None
        if target is None:
            return
        await work_refs.save_work(
            session_dir, session_dir.name, target.stage, tree, target.include,
            prefix=target.prefix, status=status, published=published, token=_work_token,
            write_message=partial(_work_message, stage.role))
    except Exception as exc:
        logger.warning('The %s work of session %s was not saved: %s', stage.role,
                       session_dir.name, exc)


async def restore_stage_output(stage: StageSpec, session_dir: Path,
                               context: dict[str, Any]) -> bool:
    """Restore the stage's published output from the repository's work refs when it is
    missing on this worker, such as after a redeploy; true when it was restored."""
    from app.github import work_refs
    destination = session_dir / stage.artifact_subdir / stage.output_subdir
    try:
        target = stage.work(context) if stage.work else None
        if target is None or await run_sync(destination.exists):
            return False
        return await work_refs.restore_work(
            session_dir, session_dir.name, target.stage, destination, target.include,
            prefix=target.prefix, token=_work_token)
    except Exception as exc:
        logger.warning('The %s output of session %s was not restored: %s', stage.role,
                       session_dir.name, exc)
        return False


async def run_stage(stage: StageSpec, session_id: str, context: dict[str, Any],
                    generated_code_dir: Path) -> dict[str, Any]:
    """The stage's published output, or `questions` the agent asked: then it is paused, and
    the same call with ANSWERS_KEY in the context continues it."""
    with trace_operation(f'build-{stage.role.replace("_", "-")}', session_id=session_id,
                         feature=stage.role.replace('_', '-'), input=context,
                         metadata=activity_metadata()) as span:
        result = await _run_stage(stage, session_id, context, generated_code_dir)
        if span is not None:
            span.update(output=result)
        return result


async def _run_stage(stage: StageSpec, session_id: str, context: dict[str, Any],
                     generated_code_dir: Path) -> dict[str, Any]:
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    answers = context.get(ANSWERS_KEY)
    resume = bool(context.get(RESUME_KEY))
    context = {key: value for key, value in context.items()
               if key not in (ANSWERS_KEY, RESUME_KEY)}
    logger.info('%s stage preparing inputs (session=%s)', stage.role, session_id)
    session_dir = generated_code_dir / session_id
    # A repair, or any run continuing the stage's published output, finds it again after
    # this worker lost it.
    await restore_stage_output(stage, session_dir, context)
    inputs = await run_sync(stage.prepare_inputs, session_dir, context)
    artifact_dir = session_dir / stage.artifact_subdir
    await run_sync(artifact_dir.mkdir, parents=True, exist_ok=True)
    paused = artifact_dir / docker_agents.PAUSED_DIR
    async with docker_agents.keep_activity_alive(stage.heartbeat):
        async with async_context(tempfile.TemporaryDirectory, dir=artifact_dir) as temporary:
            work = Path(temporary)
            workspace = work / 'workspace'
            if answers or resume:
                # The paused agent continues in the workspace it left, inputs included.
                await run_sync(docker_agents.resume_workspace, paused, workspace,
                               answers if answers else None)
            else:
                await run_sync(workspace.mkdir)
                await run_sync(inputs.populate, workspace)
                await run_sync(
                    (workspace / 'input.json').write_text,
                    json.dumps(inputs.context, ensure_ascii=False),
                )
            output = work / 'output'
            try:
                await docker_agents.run_agent(
                    stage.role, session_id, workspace, output, stage.workspace_name,
                    artifact_dir / 'agent.log', image_build=stage.image_build, paused=paused,
                )
            except docker_agents.AgentPaused as exc:
                return paused_result(session_id, exc)
            except docker_agents.AgentLimited as exc:
                logger.warning('%s stage waits for its provider (session=%s): %s',
                               stage.role, session_id, exc.report.get('message'))
                await save_stage_work(stage, session_dir, context,
                                      paused / stage.workspace_name,
                                      'stopped by the provider\'s limits')
                return limited_result(session_id, exc)
            except Exception as exc:
                await save_stage_work(stage, session_dir, context, output,
                                      f'agent failed: {_first_line(exc)}')
                raise
            try:
                return await _publish_output(stage, session_id, session_dir, context,
                                             artifact_dir, work, output)
            except Exception as exc:
                await save_stage_work(stage, session_dir, context, output,
                                      f'not published: {_first_line(exc)}')
                raise


def _first_line(exc: BaseException) -> str:
    text = next((line.strip() for line in str(exc).splitlines() if line.strip()),
                type(exc).__name__)
    return text[:200]


async def _publish_output(stage: StageSpec, session_id: str, session_dir: Path,
                          context: dict[str, Any], artifact_dir: Path, work: Path,
                          output: Path) -> dict[str, Any]:
    """Sanitize, validate, build and publish the agent's output."""
    if stage.declined_result:
        declined = await run_sync(stage.declined_result, output)
        if declined is not None:
            logger.info('%s stage declined to publish an output (session=%s)',
                        stage.role, session_id)
            return {**declined, 'session_id': session_id}
    logger.info('%s stage sanitizing agent output (session=%s)', stage.role, session_id)
    clean = work / 'build'
    await run_sync(stage.sanitize_output, output, clean)
    await run_sync(stage.validate_output, clean)
    image = {}
    if stage.image_label:
        build_log = artifact_dir / 'image-build.log'
        logger.info('%s stage building %s image (session=%s, log=%s)',
                    stage.role, stage.image_label, session_id, build_log)
        image = {'image_id': await (
            stage.build_output(clean, build_log) if stage.build_output
            else build_image(clean, stage.image_label, build_log))}
    destination = artifact_dir / stage.output_subdir
    if await run_sync(destination.exists):
        await run_sync(shutil.rmtree, destination)
    await run_sync(os.replace, clean, destination)
    result = {**image, 'path': str(destination), 'session_id': session_id}
    await run_sync(publish_manifest, artifact_dir, result)
    logger.info('%s stage published its output (session=%s, image=%s, path=%s)',
                stage.role, session_id, image.get('image_id'), destination)
    await save_stage_work(stage, session_dir, context, destination, 'built',
                          published=True)
    return result
