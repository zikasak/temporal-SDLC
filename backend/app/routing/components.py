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
from app.github.checkout import COMPONENTS_DIR, CONTRACTS_DIR
from app.languages import RUNTIME_FILE, ComponentContract

logger = logging.getLogger(__name__)

# Where the contract is published in the repository, and kept beside the routing
# (COMPONENTS_DIR).
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
            symbols = contract.read_stub(module, file.read_text())
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
    """Every symbol a `component` route names, or adds to another module, is stubbed in
    the module it names."""
    for requirement, route in routes.items():
        seam = route.get('seam')
        if route['route'] != 'component' or seam is None:
            continue
        if seam['module'] not in modules:
            raise RuntimeError(f'The seam of requirement {requirement} names a module the '
                               f'component contract has no stub of: {seam["module"]}')
        missing = [symbol for symbol in [*seam['symbols'], *seam.get('added', [])]
                   if symbol not in modules[seam['module']]]
        if missing:
            raise RuntimeError(f'The seam of requirement {requirement} names symbols its stub '
                               f'does not define: {", ".join(missing)[:400]}')
        for module, symbols in (seam.get('added_elsewhere') or {}).items():
            if module not in modules:
                raise RuntimeError(f'The seam of requirement {requirement} adds symbols to a '
                                   f'module the component contract has no stub of: {module}')
            missing = [symbol for symbol in symbols if symbol not in modules[module]]
            if missing:
                raise RuntimeError(f'The seam of requirement {requirement} adds symbols to '
                                   f'{module} its stub does not define: '
                                   f'{", ".join(missing)[:400]}')


def stub_modules(contract: ComponentContract, components: Path) -> list[str]:
    """The modules a checked component contract has stubs of."""
    return sorted(contract.module(path.relative_to(components))
                  for path in components.rglob(f'*{contract.stub_suffix}')
                  if path.is_file() and not path.is_symlink())


def _context(contract: ComponentContract, repository: Path, components: Path,
             context: Path) -> None:
    shutil.copytree(contract.conformance, context)
    copy_regular_files(repository, context / 'repository', include=lambda relative: True)
    # Validated already: regular files only.
    shutil.copytree(components, context / 'components')


# One difference as stubtest (and the literal alias check) reports it: the symbol first.
_DIFFERENCE = re.compile(r'^([A-Za-z_][\w.]*) (is inconsistent|is not present|variable differs'
                         r'|is not a )')
DIFFERENCE_CHARS = 300


def describe_differences(output: str) -> str:
    """What a failed check says, led by the symbols that differ: each named once, and the
    differences that name what one side lacks first, since a parameter a stub lacks makes
    every later one look renamed. `__match_args__` of a class whose `__init__` differs says
    the same again, so it is left out. Output in no known format is kept as it is, its end
    when too long."""
    found: dict[str, list[str]] = {}
    for line in output.splitlines():
        line = line.strip()
        if match := _DIFFERENCE.match(line):
            found.setdefault(match.group(1), []).append(line)
    if not found:
        return output[-MESSAGE_CHARS:]
    symbols = [symbol for symbol in found
               if not (symbol.endswith('.__match_args__')
                       and f'{symbol.removesuffix(".__match_args__")}.__init__' in found)]
    lines = sorted((line for symbol in symbols for line in found[symbol]),
                   key=lambda line: 'does not have' not in line and 'not present' not in line)
    message = (f'{len(symbols)} symbol(s) differ: {", ".join(symbols)}. '
               + ' | '.join(line if len(line) <= DIFFERENCE_CHARS
                            else line[:DIFFERENCE_CHARS - 1] + '…' for line in lines))
    return message if len(message) <= MESSAGE_CHARS else message[:MESSAGE_CHARS - 1] + '…'


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
                           f'{describe_differences(str(exc).removeprefix("Docker run failed: "))}'
                           ) from exc
    finally:
        try:
            await docker_agents.docker('image', 'rm', '-f', image, timeout=60)
        except Exception:
            logger.warning('Could not remove the conformance image %s', image)
    logger.info('Component contract conforms (modules=%s)', sorted(modules))


async def check_published(contract: ComponentContract, published: Path, where: str,
                          repository: Path, work: Path, log_dir: Path) -> None:
    """Check the contract the repository publishes (`published`, at `where` in it) against `repository`, the
    code the delivery started from, before a routing extends it: a stub the code no longer
    matches would otherwise be found only once the whole contract is checked, after a
    seam is added. Nothing is checked when nothing is published. Raises RuntimeError
    saying the published contract is out of date otherwise."""
    files = await run_sync(_files, published)
    if RUNTIME_FILE not in files or not any(path.endswith(contract.stub_suffix)
                                            for path in files):
        return
    clean = work / 'published'
    try:
        read = await run_sync(read_components, contract, published, repository, published,
                              clean)
        await check_conformance(contract, repository, clean, list(read['modules']), work,
                                log_dir)
    except RuntimeError as exc:
        raise RuntimeError(
            f'The component contract the repository publishes in {where}/ no longer '
            'matches its code, so the routing cannot extend it. Bring those stubs up to date '
            f'with the code in the repository and start the delivery again. {exc}') from exc
