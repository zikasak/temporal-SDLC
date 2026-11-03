"""Host-only reading and checking of a component contract: the stubs of the seams at which
criteria the HTTP contract tests cannot observe are observed instead."""
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from app import docker_agents
from app.agent_workflows.host import copy_file, copy_regular_files
from app.asyncio_utils import run_sync
from app.github.checkout import CONTRACTS_DIR
from app.languages import RUNTIME_FILE, ComponentContract

logger = logging.getLogger(__name__)

# Where the contract is published in the repository, and kept beside the routing.
COMPONENTS_DIR = 'components'
PUBLISHED_DIR = Path(CONTRACTS_DIR) / COMPONENTS_DIR
# Where the routing keeps the component contract it checked against the code, in the
# session's artifacts: the stubs every later stage of the delivery is given.
ROUTED_CONTRACT = Path('routing') / COMPONENTS_DIR
MAX_STUBS = 50
MAX_STUB_BYTES = 256 * 1024
MAX_RUNTIME_BYTES = 16 * 1024
CHECK_SECONDS = 420
# How the conformance check is confined: no network, as the component tests will run.
CONSTRAINTS = ('--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
               '--pids-limit=256', '--memory=4g', '--cpus=2', '--user', '10001:10001')
MESSAGE_CHARS = 4000


def _files(root: Path) -> dict[str, Path]:
    """The regular files under `root` by relative path; a link anywhere is rejected."""
    if not root.is_dir() or root.is_symlink():
        return {}
    files: dict[str, Path] = {}
    for item in sorted(root.rglob('*')):
        if item.is_symlink():
            raise RuntimeError(f'The component contract holds a link: '
                               f'{item.relative_to(root).as_posix()[:200]}')
        if item.is_file():
            files[item.relative_to(root).as_posix()] = item
    return files


def _runtime(contract: ComponentContract, file: Path, repository: Path) -> dict[str, Any]:
    if file.stat().st_size > MAX_RUNTIME_BYTES:
        raise RuntimeError(f'The component contract\'s {RUNTIME_FILE} is too large')
    try:
        return contract.read_runtime(json.loads(file.read_text()), repository)
    except ValueError as exc:
        raise RuntimeError(f'The component contract\'s {RUNTIME_FILE} is unusable: '
                           f'{exc}') from exc


def read_components(contract: ComponentContract, output: Path, repository: Path,
                    published: Path, target: Path) -> dict[str, Any]:
    """Copy the agent's untrusted component contract from `output` to `target`: its runtime
    file and stubs, each stub only a description of existing symbols. Files of the
    published contract (`published`) are kept, and a published stub only gains
    statements. `repository` is the code the runtime file names paths of. Returns the
    normalized runtime and the symbols each stubbed module defines."""
    files = _files(output)
    stubs = {path: file for path, file in files.items()
             if path.endswith(contract.stub_suffix)}
    if RUNTIME_FILE not in files:
        raise RuntimeError(f'The component contract has no {RUNTIME_FILE}')
    if not stubs:
        raise RuntimeError('The component contract has no stub')
    if len(stubs) > MAX_STUBS:
        raise RuntimeError(f'The component contract has more than {MAX_STUBS} stubs')
    runtime = _runtime(contract, files[RUNTIME_FILE], repository)
    modules: dict[str, set[str]] = {}
    for path, file in stubs.items():
        if file.stat().st_size > MAX_STUB_BYTES:
            raise RuntimeError(f'The stub {path} is too large')
        try:
            module = contract.module(Path(path))
            symbols = contract.read_stub(file.read_text())
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(f'The stub {path} is not a stub: {exc}') from exc
        if module in modules:
            raise RuntimeError(f'The module {module} has two stubs')
        modules[module] = symbols
    for path, file in _files(published).items():
        if path == RUNTIME_FILE:
            if _runtime(contract, file, repository) != runtime:
                raise RuntimeError(f'The published {RUNTIME_FILE} of the component contract '
                                   'was changed')
        elif path.endswith(contract.stub_suffix):
            if path not in stubs:
                raise RuntimeError(f'The published stub {path} was removed')
            kept = contract.statements(stubs[path].read_text())
            if any(statement not in kept for statement in contract.statements(file.read_text())):
                raise RuntimeError(f'A statement of the published stub {path} was changed or '
                                   'removed; a published stub may only gain statements')
    target.mkdir(parents=True)
    for path in [RUNTIME_FILE, *stubs]:
        (target / path).parent.mkdir(parents=True, exist_ok=True)
        copy_file(files[path], target / path)
    return {'runtime': runtime, 'modules': modules}


def check_seams(routes: dict[str, dict[str, Any]], modules: dict[str, set[str]]) -> None:
    """Every symbol a `component` route names is stubbed in the module it names."""
    for requirement, route in routes.items():
        seam = route.get('seam')
        if route['route'] != 'component' or seam is None:
            continue
        if seam['module'] not in modules:
            raise RuntimeError(f'The seam of requirement {requirement} names a module the '
                               f'component contract has no stub of: {seam["module"]}')
        missing = [symbol for symbol in seam['symbols']
                   if symbol not in modules[seam['module']]]
        if missing:
            raise RuntimeError(f'The seam of requirement {requirement} names symbols its stub '
                               f'does not define: {", ".join(missing)[:400]}')


def _context(contract: ComponentContract, repository: Path, components: Path,
             context: Path) -> None:
    shutil.copytree(contract.conformance, context)
    copy_regular_files(repository, context / 'repository', include=lambda relative: True)
    # Validated already: regular files only.
    shutil.copytree(components, context / 'components')


async def check_conformance(contract: ComponentContract, repository: Path, components: Path,
                            modules: list[str], work: Path, log_dir: Path) -> None:
    """Check, without a network, that the stubs in `components` still describe the code
    in `repository`: every stubbed symbol exists there with the stubbed signature. Raises
    RuntimeError naming the differences otherwise."""
    context = work / 'conformance'
    await run_sync(_context, contract, repository, components, context)
    runtime = json.loads(await run_sync((components / RUNTIME_FILE).read_text))
    # The build arguments come from the runtime file read_components normalized.
    build = ['build', *[argument for key, value in contract.build_args(runtime).items()
                        for argument in ('--build-arg', f'{key}={value}')]]
    iid = work / 'image-id'
    try:
        await docker_agents.docker(*build, '--iidfile', str(iid), str(context),
                                   log_path=log_dir / 'conformance-build.log')
    except RuntimeError as exc:
        raise RuntimeError('The code the component contract describes could not be installed '
                           f'for its check: {str(exc)[-MESSAGE_CHARS:]}') from exc
    image = (await run_sync(iid.read_text)).strip()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise RuntimeError('Docker returned an invalid conformance image ID')
    try:
        await docker_agents.docker('run', '--rm', *CONSTRAINTS, image, *sorted(modules),
                                   timeout=CHECK_SECONDS, log_path=log_dir / 'conformance.log')
    except RuntimeError as exc:
        raise RuntimeError('The component contract does not match the code: '
                           f'{str(exc).removeprefix("Docker run failed: ")[-MESSAGE_CHARS:]}'
                           ) from exc
    finally:
        try:
            await docker_agents.docker('image', 'rm', '-f', image, timeout=60)
        except Exception:
            logger.warning('Could not remove the conformance image %s', image)
    logger.info('Component contract conforms (modules=%s)', sorted(modules))
