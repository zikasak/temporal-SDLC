"""Host-only prepare/run/build/publish lifecycle with explicit stage policies."""
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
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


# The language's authoring instructions, placed beside the agent's project.
LANGUAGE_FILENAME = 'language.json'
# Context entry with the user's answers, which continues the stage's paused agent.
ANSWERS_KEY = 'answers'


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


async def build_image(context: Path, label: str, log_path: Path) -> str:
    """Build the image of a sanitized source tree and return its immutable ID."""
    iid = context.parent / 'image-id'
    await docker_agents.docker('build', '--iidfile', str(iid), str(context), log_path=log_path)
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
    context = {key: value for key, value in context.items() if key != ANSWERS_KEY}
    logger.info('%s stage preparing inputs (session=%s)', stage.role, session_id)
    session_dir = generated_code_dir / session_id
    inputs = await run_sync(stage.prepare_inputs, session_dir, context)
    artifact_dir = session_dir / stage.artifact_subdir
    await run_sync(artifact_dir.mkdir, parents=True, exist_ok=True)
    paused = artifact_dir / docker_agents.PAUSED_DIR
    async with docker_agents.keep_activity_alive(stage.heartbeat):
        async with async_context(tempfile.TemporaryDirectory, dir=artifact_dir) as temporary:
            work = Path(temporary)
            workspace = work / 'workspace'
            if answers:
                # The paused agent continues in the workspace it left, inputs included.
                await run_sync(docker_agents.resume_workspace, paused, workspace, answers)
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
            logger.info('%s stage sanitizing agent output (session=%s)', stage.role, session_id)
            clean = work / 'build'
            await run_sync(stage.sanitize_output, output, clean)
            await run_sync(stage.validate_output, clean)
            image = {}
            if stage.image_label:
                build_log = artifact_dir / 'image-build.log'
                logger.info('%s stage building %s image (session=%s, log=%s)',
                            stage.role, stage.image_label, session_id, build_log)
                image = {'image_id': await build_image(clean, stage.image_label, build_log)}
            destination = artifact_dir / stage.output_subdir
            if await run_sync(destination.exists):
                await run_sync(shutil.rmtree, destination)
            await run_sync(os.replace, clean, destination)
            result = {**image, 'path': str(destination), 'session_id': session_id}
            await run_sync(publish_manifest, artifact_dir, result)
            logger.info('%s stage published its output (session=%s, image=%s, path=%s)',
                        stage.role, session_id, image.get('image_id'), destination)
            return result
