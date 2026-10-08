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
    image_label: str
    image_build: docker_agents.AgentImageBuild
    prepare_inputs: Callable[[Path, dict[str, Any]], StageInputs]
    sanitize_output: Callable[[Path, Path], None]
    validate_output: Callable[[Path], None]


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


async def run_stage(stage: StageSpec, session_id: str, context: dict[str, Any],
                    generated_code_dir: Path) -> dict[str, Any]:
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
    logger.info('%s stage preparing inputs (session=%s)', stage.role, session_id)
    session_dir = generated_code_dir / session_id
    inputs = stage.prepare_inputs(session_dir, context)
    artifact_dir = session_dir / stage.artifact_subdir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    async with docker_agents.keep_activity_alive(stage.heartbeat):
        with tempfile.TemporaryDirectory(dir=artifact_dir) as temporary:
            work = Path(temporary)
            workspace = work / 'workspace'
            workspace.mkdir()
            inputs.populate(workspace)
            (workspace / 'input.json').write_text(json.dumps(inputs.context, ensure_ascii=False))
            output = work / 'output'
            await docker_agents.run_agent(
                stage.role, session_id, workspace, output, stage.workspace_name,
                artifact_dir / 'agent.log', image_build=stage.image_build,
            )
            logger.info('%s stage sanitizing agent output (session=%s)', stage.role, session_id)
            clean = work / 'build'
            stage.sanitize_output(output, clean)
            stage.validate_output(clean)
            iid = work / 'image-id'
            build_log = artifact_dir / 'image-build.log'
            logger.info('%s stage building %s image (session=%s, log=%s)',
                        stage.role, stage.image_label, session_id, build_log)
            await docker_agents.docker('build', '--iidfile', str(iid), str(clean),
                                       log_path=build_log)
            image_id = iid.read_text().strip()
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
                raise RuntimeError(f'Docker returned an invalid {stage.image_label} image ID')
            destination = artifact_dir / stage.output_subdir
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(clean, destination)
            result = {'image_id': image_id, 'path': str(destination), 'session_id': session_id}
            manifest = artifact_dir / f'{uuid.uuid4().hex}.json'
            manifest.write_text(json.dumps(result, indent=2))
            os.replace(manifest, artifact_dir / 'image.json')
            logger.info('%s stage published image (session=%s, image=%s, path=%s)',
                        stage.role, session_id, image_id, destination)
            return result
