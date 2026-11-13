"""Host-only routing of a change's criteria to the suite that can prove them."""
import dataclasses
import json
import logging
import os
import re
import shutil
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
    ANSWERS_KEY, RESUME_KEY, limited_result, copy_file, copy_regular_files, paused_result, session_language, write_language,
)
from app.config import GENERATED_CODE_DIR
from app.github.checkout import CHECKOUT_DIR, COMPONENTS_DIR
from app.implementation.activities import EXCLUDED
from app.languages import RUNTIME_FILE, TOOLS_DIR, Language
from app.requirements.document import without_repeated_tables
from app.review.activities import record_baseline
from app.routing.agent import ROUTES
from app.routing.components import check_conformance, check_seams, read_components
from app.routing.models import (
    ROUTES as ALL_ROUTES, ROUTING_ERROR, added_seams, read_routes,
)
from app.routing.parts import (
    PARTS_DIR, PARTS_KEY, SERVICE_PART, ComponentPart, component_parts,
)
from app.temporal.tracing import activity_metadata
from app.tracing import trace_operation

logger = logging.getLogger(__name__)

MAX_ROUTES_BYTES = 256 * 1024


def brief_ids(context: dict[str, Any]) -> list[str]:
    return [item['id'] for item in (context.get('brief') or {}).get('requirements', [])]


def _repository_file(language: Language, parts: list[ComponentPart], relative: Path) -> bool:
    """Whether the routing agent sees a file of the checkout: never Git data or the build
    output of the service or of another part."""
    if any(part.lower() in EXCLUDED | set(language.build_dirs) for part in relative.parts):
        return False
    for part in parts:
        if not part.is_service and relative.is_relative_to(part.root) and any(
                name.lower() in part.language.build_dirs
                for name in relative.relative_to(part.root).parts):
            return False
    return True


def prepare_routing(language: Language, session_dir: Path, context: dict[str, Any],
                    workspace: Path, parts: list[ComponentPart]) -> None:
    """The repository as the delivery checked it out, without its Git data or build
    output, the contracts with the brief, and the published component contract of each
    part with its language's instructions and tools."""
    checkout = session_dir / CHECKOUT_DIR
    if not checkout.is_dir():
        raise ApplicationError('The repository checkout is unavailable', type=ROUTING_ERROR,
                               non_retryable=True)
    if not brief_ids(context):
        raise ApplicationError('The change has no requirements to route', type=ROUTING_ERROR,
                               non_retryable=True)
    copy_regular_files(checkout, workspace / 'routing' / 'repository',
                       include=lambda relative: _repository_file(language, parts, relative))
    contracts = without_repeated_tables(
        {key: value for key, value in context.items() if key not in ('language', 'repository')})
    index, files = on_demand_contracts(contracts, len(json.dumps(contracts))) or (contracts, {})
    others = [part for part in parts if not part.is_service]
    if others:
        index = {**index, 'parts': [part.describe() for part in parts]}
    for part in parts:
        if part.contract is None:
            continue
        # The published component contract, which the agent extends rather than replaces.
        published = checkout / part.published_dir()
        if published.is_dir():
            copy_regular_files(published, workspace / 'routing' / part.contract_dir(),
                               include=lambda relative: True)
        for tool in part.contract.tools:
            (workspace / TOOLS_DIR).mkdir(exist_ok=True)
            copy_file(tool, workspace / TOOLS_DIR / tool.name)
    component = language.component_contract
    part_components = {part.language.name: part.contract.instructions.format(
        runtime=RUNTIME_FILE) for part in others}
    write_language(workspace, language, label=language.label, components=(
        component.instructions.format(runtime=RUNTIME_FILE) if component else ''),
        **({'part_components': part_components} if part_components else {}))
    if files:
        write_contracts(workspace, files)
    (workspace / 'input.json').write_text(json.dumps(index, ensure_ascii=False))


def read_output(output: Path, requirement_ids: list[str], language: Language,
                parts: list[ComponentPart]) -> dict[str, dict[str, Any]]:
    """The agent's output is untrusted: accept one bounded, complete routing, with the
    `component` route only at a seam of a part whose language has component contracts."""
    file = output / ROUTES
    if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_ROUTES_BYTES:
        raise RuntimeError('Routing did not produce routes')
    try:
        data = json.loads(file.read_text())
    except ValueError as exc:
        raise RuntimeError('The routes are not valid JSON') from exc
    others = tuple(part.name for part in parts if not part.is_service)
    components = bool(language.component_contract) or bool(others)
    routes = read_routes(data, requirement_ids, ALL_ROUTES if components else
                         tuple(route for route in ALL_ROUTES if route != 'component'), others)
    if language.component_contract is None:
        for requirement, route in routes.items():
            if route['route'] == 'component' and 'part' not in route['seam']:
                raise RuntimeError(f'Requirement {requirement} is routed to a seam of the '
                                   f'service, but {language.label} services have no component '
                                   'contracts')
    return routes


def _part_routes(part: ComponentPart, routes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {requirement: route for requirement, route in routes.items()
            if route['route'] == 'component'
            and route['seam'].get('part', SERVICE_PART) == part.name}


async def _publish_part(part: ComponentPart, session_dir: Path, output: Path,
                        routes: dict[str, dict[str, Any]], repository: Path, work: Path,
                        artifact_dir: Path) -> None:
    contract = part.contract
    clean = work / part.contract_dir()
    read = await run_sync(read_components, contract, output / part.contract_dir(), repository,
                          session_dir / CHECKOUT_DIR / part.published_dir(), clean)
    if not part.is_service and read['runtime'].get('project') != part.root:
        raise RuntimeError(f'Its {RUNTIME_FILE} names the project '
                           f'{read["runtime"].get("project")}, not the part\'s directory '
                           f'{part.root}')
    check_seams(routes, read['modules'])
    added = added_seams(routes)
    if added:
        # Symbols the change adds first cannot match the code yet: the whole contract is
        # checked against the code once they are added, before anything runs through them.
        logger.info('The component contract of part %s names symbols the change adds; its '
                    'check waits for them (modules=%s)', part.name, sorted(added))
    else:
        log_dir = artifact_dir if part.is_service else artifact_dir / PARTS_DIR / part.name
        check = work / 'checks' / part.name
        await run_sync(check.mkdir, parents=True)
        await check_conformance(contract, repository, clean, list(read['modules']), check,
                                log_dir)
    target = artifact_dir / part.contract_dir()
    await run_sync(target.parent.mkdir, parents=True, exist_ok=True)
    await run_sync(shutil.move, clean, target)


async def publish_components(language: Language, session_dir: Path, output: Path,
                             routes: dict[str, dict[str, Any]], work: Path,
                             artifact_dir: Path, parts: list[ComponentPart]) -> list[str]:
    """Validate the component contract the agent wrote for the `component` routes of each
    part and check it against the code the delivery started from, then keep it beside the
    routing. Returns the names of the parts with routes, in the order of `parts`."""
    # An earlier routing's contracts are never mistaken for this one's.
    for name in (COMPONENTS_DIR, PARTS_DIR):
        await run_sync(shutil.rmtree, artifact_dir / name, ignore_errors=True)
    routed = [(part, part_routes) for part in parts
              if (part_routes := _part_routes(part, routes))]
    if not routed:
        return []
    repository = await run_sync(record_baseline, language, session_dir)
    for part, part_routes in routed:
        try:
            await _publish_part(part, session_dir, output, part_routes, repository, work,
                                artifact_dir)
        except RuntimeError as exc:
            if part.is_service:
                raise
            raise RuntimeError(f'The component contract of part {part.name} is unusable: '
                               f'{exc}') from exc
    return [part.name for part, _ in routed]


async def run_routing(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    """The routes, or `questions` the agent asked: then it is paused, and the same call
    with ANSWERS_KEY in the context continues it."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    answers = context.get(ANSWERS_KEY)
    # An agent its provider's limits stopped continues where it was, as a paused one does.
    resume = bool(context.get(RESUME_KEY))
    context = {key: value for key, value in context.items()
               if key not in (ANSWERS_KEY, RESUME_KEY)}
    language = session_language(context.get('language'))
    session_dir = GENERATED_CODE_DIR / session_id
    artifact_dir = session_dir / 'routing'
    checkout = session_dir / CHECKOUT_DIR
    parts = (await run_sync(component_parts, language, checkout) if checkout.is_dir()
             else [ComponentPart(SERVICE_PART, '.', language)])
    paused = artifact_dir / docker_agents.PAUSED_DIR
    async with async_context(tempfile.TemporaryDirectory) as temporary:
        work = Path(temporary)
        workspace = work / 'workspace'
        await run_sync(artifact_dir.mkdir, parents=True, exist_ok=True)
        if answers or resume:
            await run_sync(docker_agents.resume_workspace, paused, workspace,
                           answers or None)
        else:
            await run_sync(workspace.mkdir)
            await run_sync(prepare_routing, language, session_dir, context, workspace,
                           parts)
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
            except docker_agents.AgentLimited as exc:
                return limited_result(session_id, exc)
            except docker_agents.AgentCredentialsError as exc:
                # Routing again cannot help until the settings or the worker change.
                raise ApplicationError(str(exc), type=ROUTING_ERROR,
                                       non_retryable=True) from exc
        try:
            routes = await run_sync(read_output, output, brief_ids(context), language, parts)
            async with docker_agents.keep_activity_alive('Checking the component contract'):
                routed = await publish_components(language, session_dir, output, routes,
                                                  work, artifact_dir, parts)
        except RuntimeError as exc:
            # The same routing fails the same way again: the user is told why.
            raise ApplicationError(str(exc), type=ROUTING_ERROR, non_retryable=True) from exc
    components = artifact_dir / COMPONENTS_DIR
    # The parts with routes, which the stages after the routing read from this file.
    result = {'session_id': session_id, 'routes': routes, 'path': str(artifact_dir),
              PARTS_KEY: [part.record() for part in parts if part.name in routed],
              **({'components': str(components)} if SERVICE_PART in routed else {})}
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
