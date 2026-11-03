"""Host-only routing of a change's criteria to the suite that can prove them."""
import dataclasses
import json
import logging
import os
import re
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
from app.github.checkout import CHECKOUT_DIR
from app.implementation.activities import EXCLUDED
from app.languages import Language
from app.requirements.document import without_repeated_tables
from app.routing.agent import ROUTES
from app.routing.models import read_routes
from app.temporal.tracing import activity_metadata
from app.tracing import trace_operation

logger = logging.getLogger(__name__)

MAX_ROUTES_BYTES = 256 * 1024


def brief_ids(context: dict[str, Any]) -> list[str]:
    return [item['id'] for item in (context.get('brief') or {}).get('requirements', [])]


def prepare_routing(language: Language, session_dir: Path, context: dict[str, Any],
                    workspace: Path) -> None:
    """The repository as the delivery checked it out, without its Git data or build
    output, and the contracts with the brief."""
    checkout = session_dir / CHECKOUT_DIR
    if not checkout.is_dir():
        raise ApplicationError('The repository checkout is unavailable', non_retryable=True)
    if not brief_ids(context):
        raise ApplicationError('The change has no requirements to route', non_retryable=True)
    excluded = EXCLUDED | set(language.build_dirs)
    copy_regular_files(checkout, workspace / 'routing' / 'repository', include=lambda relative: (
        not any(part.lower() in excluded for part in relative.parts)))
    contracts = without_repeated_tables(
        {key: value for key, value in context.items() if key not in ('language', 'repository')})
    index, files = on_demand_contracts(contracts, len(json.dumps(contracts))) or (contracts, {})
    write_language(workspace, language, label=language.label)
    if files:
        write_contracts(workspace, files)
    (workspace / 'input.json').write_text(json.dumps(index, ensure_ascii=False))


def read_output(output: Path, requirement_ids: list[str]) -> dict[str, dict[str, str]]:
    """The agent's output is untrusted: accept one bounded, complete routing."""
    file = output / ROUTES
    if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_ROUTES_BYTES:
        raise RuntimeError('Routing did not produce routes')
    try:
        data = json.loads(file.read_text())
    except ValueError as exc:
        raise RuntimeError('The routes are not valid JSON') from exc
    return read_routes(data, requirement_ids)


async def run_routing(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    """The routes, or `questions` the agent asked: then it is paused, and the same call
    with ANSWERS_KEY in the context continues it."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    answers = context.get(ANSWERS_KEY)
    context = {key: value for key, value in context.items() if key != ANSWERS_KEY}
    language = session_language(context.get('language'))
    session_dir = GENERATED_CODE_DIR / session_id
    artifact_dir = session_dir / 'routing'
    paused = artifact_dir / docker_agents.PAUSED_DIR
    async with async_context(tempfile.TemporaryDirectory) as temporary:
        work = Path(temporary)
        workspace = work / 'workspace'
        await run_sync(artifact_dir.mkdir, parents=True, exist_ok=True)
        if answers:
            await run_sync(docker_agents.resume_workspace, paused, workspace, answers)
        else:
            await run_sync(workspace.mkdir)
            await run_sync(prepare_routing, language, session_dir, context, workspace)
        async with docker_agents.keep_activity_alive('Routing the acceptance criteria'):
            output = work / 'output'
            try:
                await docker_agents.run_agent(
                    'routing', session_id, workspace, output, 'routing',
                    artifact_dir / 'agent.log', paused=paused,
                    image_build=dataclasses.replace(
                        docker_agents.default_agent_image('routing', 'routing'),
                        dockerfile=language.agent_dockerfile))
            except docker_agents.AgentPaused as exc:
                return paused_result(session_id, exc)
        routes = await run_sync(read_output, output, brief_ids(context))
    result = {'session_id': session_id, 'routes': routes, 'path': str(artifact_dir)}
    manifest = artifact_dir / f'{uuid.uuid4().hex}.json'
    await run_sync(
        manifest.write_text, json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    await run_sync(os.replace, manifest, artifact_dir / ROUTES)
    logger.info('Routing finished (session=%s, routes=%s)', session_id,
                {key: value['route'] for key, value in routes.items()})
    return result


@activity.defn
async def route_requirements(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    with trace_operation('route-requirements', session_id=session_id, feature='routing',
                         input=context.get('brief'), metadata=activity_metadata()) as span:
        result = await run_routing(session_id, context)
        if span is not None:
            span.update(output=result)
        return result
