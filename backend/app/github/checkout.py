"""Host-side Git working copy of a session's repository.

The token reaches Git only through its environment: never argv, the working copy's
configuration, logs or error messages. Repository content is untrusted: hooks and
host Git configuration are disabled and links are checked out as plain files.
"""

import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path

from app.asyncio_utils import async_context, run_sync
from app.agent_workflows.host import copy_file
from app.github.client import validate_selection
from app.github.models import RepositoryCheckout, RepositoryPublication, RepositorySelection

# Beside a session's generated artifacts.
CHECKOUT_DIR = 'repository'
MANIFEST = 'repository.json'
# The independent contract tests' directory inside the repository.
TESTS_DIR = 'contract-tests'
# The component tests' directory inside the repository: criteria observed at seams of the
# code rather than over HTTP.
COMPONENT_TESTS_DIR = 'component-tests'
# The session's copy of the repository's tests as its delivery found them, taken before
# any author runs: what later stages compare the suite with.
SNAPSHOT_DIR = 'repository-tests'
# Where, inside it, the repository's component tests are recorded; its contract tests are
# in `suite`.
COMPONENT_SNAPSHOT = 'components'
# The published contract the repository's later tasks start from.
CONTRACTS_DIR = 'contracts'
# Where, inside it, the component contract is: the stubs of the seams component tests use.
COMPONENTS_DIR = 'components'
# Where, inside it, the contracts of the repository's other parts are: contracts/parts/P/.
CONTRACT_PARTS_DIR = 'parts'
# A part's page contract, beside its component contract: contracts/parts/P/browser/.
BROWSER_DIR = 'browser'
# Where a part keeps its browser tests: P/browser-tests/.
BROWSER_TESTS_DIR = 'browser-tests'
BRANCH_PREFIX = 'generated/'
GITHUB_URL = 'https://github.com/'
GIT_TIMEOUT = 540
OUTPUT_CHARS = 1500
LISTED_IGNORED = 20
# How much of what conflicting remote commits changed a fix round is shown.
CONFLICT_CHARS = 30000


class CheckoutError(RuntimeError):
    pass


def checkout_path(base: Path, session_id: str) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise CheckoutError('Invalid session ID')
    return base / session_id / CHECKOUT_DIR


def publication_branch(session_id: str) -> str:
    """Named after the session, whichever kind of session started the delivery."""
    return BRANCH_PREFIX + session_id.rpartition('-session-')[2][:12]


def _credentials(token: str) -> str:
    return base64.b64encode(f'x-access-token:{token}'.encode()).decode()


def _environment(token: str | None) -> dict[str, str]:
    settings = {
        # An empty value clears helpers that could prompt or store the token.
        'credential.helper': '',
        'core.hooksPath': os.devnull,
        'core.symlinks': 'false',
        'commit.gpgSign': 'false',
    }
    if token:
        settings[f'http.{GITHUB_URL}.extraheader'] = (
            f'Authorization: Basic {_credentials(token)}')
    environment = {
        **os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_COUNT': str(len(settings)),
    }
    for index, (key, value) in enumerate(settings.items()):
        environment[f'GIT_CONFIG_KEY_{index}'] = key
        environment[f'GIT_CONFIG_VALUE_{index}'] = value
    return environment


async def _git(*args: str, cwd: Path, token: str | None = None,
               env: dict[str, str] | None = None) -> str:
    process = await asyncio.create_subprocess_exec(
        'git', *args, cwd=cwd, env={**_environment(token), **(env or {})},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    text = output.decode('utf-8', 'replace').strip()
    if token:
        text = (text.replace(token, '[redacted]').replace(_credentials(token), '[redacted]'))
    if process.returncode:
        raise CheckoutError(f'git {args[0]} failed: {text[-OUTPUT_CHARS:]}')
    return text


def checked_out_commit(base: Path, session_id: str) -> str:
    """The commit the session's working copy holds; empty without one."""
    try:
        manifest = json.loads((checkout_path(base, session_id).parent / MANIFEST).read_text())
    except (OSError, ValueError):
        return ''
    commit = manifest.get('commit') if isinstance(manifest, dict) else None
    return commit if isinstance(commit, str) else ''


def _recorded(session_dir: Path, repository: RepositorySelection) -> RepositoryCheckout | None:
    """The earlier checkout of this same selection, if it is still on disk."""
    try:
        manifest = json.loads((session_dir / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    if (not (session_dir / CHECKOUT_DIR / '.git').is_dir() or not isinstance(manifest, dict)
            or any(manifest.get(key) != repository[key] for key in ('full_name', 'branch'))
            or not re.fullmatch(r'[0-9a-f]{40,64}', str(manifest.get('commit')))):
        return None
    return {'commit': manifest['commit']}


async def checkout(session_dir: Path, repository: RepositorySelection,
                   token: str, *, latest: bool = False) -> RepositoryCheckout:
    """Clone the selected branch into the session, once: a repeated call (an activity
    retry, a restarted run) keeps the existing working copy and whatever it holds. With
    `latest` the branch is cloned again as it is now, replacing that copy only once the
    new one is complete."""
    validate_selection(repository['full_name'], repository['branch'])
    recorded = None if latest else await run_sync(_recorded, session_dir, repository)
    if recorded:
        return recorded
    await run_sync(session_dir.mkdir, parents=True, exist_ok=True)
    target = session_dir / CHECKOUT_DIR
    async with async_context(tempfile.TemporaryDirectory, dir=session_dir) as temporary:
        clone = Path(temporary) / CHECKOUT_DIR
        await _git(
            'clone', '--quiet', '--single-branch', '--no-tags', '--depth', '1',
            '--config', 'core.symlinks=false', '--branch', repository['branch'], '--',
            f'{GITHUB_URL}{repository["full_name"]}.git', str(clone),
            cwd=session_dir, token=token,
        )
        commit = await _git('rev-parse', 'HEAD', cwd=clone)
        if await run_sync(target.exists):
            await run_sync(shutil.rmtree, target)
        await run_sync(os.replace, clone, target)
        manifest = Path(temporary) / MANIFEST
        await run_sync(manifest.write_text, json.dumps({'full_name': repository['full_name'],
                                        'branch': repository['branch'], 'commit': commit}))
        await run_sync(os.replace, manifest, session_dir / MANIFEST)
    return {'commit': commit}


async def snapshot(session_dir: Path, repository: RepositorySelection, commit: str,
                   target: Path, token: Callable[[], Awaitable[str]]) -> None:
    """Write the files of `commit` to the empty directory `target`, a copy of its own that
    later changes of the session's working copy leave as it is. The commit is read from
    the working copy that pushed it, or else fetched from GitHub."""
    validate_selection(repository['full_name'], repository['branch'])
    if not re.fullmatch(r'[0-9a-f]{40,64}', commit):
        raise CheckoutError('Invalid commit')
    source = session_dir / CHECKOUT_DIR
    await _git('init', '--quiet', str(target), cwd=target.parent)
    fetched = False
    if await run_sync((source / '.git').is_dir):
        try:
            # A local fetch by commit, with the objects copied, not shared.
            await _git('-c', 'uploadpack.allowAnySHA1InWant=true', 'fetch', '--quiet',
                       '--no-tags', '--depth', '1', f'file://{source.resolve()}', commit,
                       cwd=target)
            fetched = True
        except CheckoutError:
            fetched = False
    if not fetched:
        await _git('fetch', '--quiet', '--no-tags', '--depth', '1',
                   f'{GITHUB_URL}{repository["full_name"]}.git', commit,
                   cwd=target, token=await token())
    await _git('-c', 'core.symlinks=false', 'checkout', '--quiet', '--detach', commit,
               cwd=target)
    # Only the files are read; the copy keeps no Git metadata.
    await run_sync(shutil.rmtree, target / '.git')


async def delivery_diff(session_dir: Path, commit: str, limit: int) -> str:
    """What the delivery changed: the diff of the pushed `commit` against the commit the
    session's working copy was checked out at, its file summary first, cut at `limit`
    characters. Raises CheckoutError when either commit cannot be read."""
    if not re.fullmatch(r'[0-9a-f]{40,64}', commit):
        raise CheckoutError('Invalid commit')
    try:
        manifest = json.loads(await run_sync((session_dir / MANIFEST).read_text))
    except (OSError, ValueError) as exc:
        raise CheckoutError('The checkout the delivery started from is unknown') from exc
    base = manifest.get('commit') if isinstance(manifest, dict) else None
    if not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{40,64}', base):
        raise CheckoutError('The checkout the delivery started from is unknown')
    source = session_dir / CHECKOUT_DIR
    if not await run_sync((source / '.git').is_dir):
        raise CheckoutError('The repository checkout is unavailable')
    stat = await _git('diff', '--no-color', '--no-ext-diff', '--stat=200', base, commit,
                      cwd=source)
    patch = await _git('diff', '--no-color', '--no-ext-diff', '--unified=2', base, commit,
                       cwd=source)
    text = f'{stat}\n\n{patch}'
    return text if len(text) <= limit else text[:limit] + '\n… (the diff goes on)'


class PushConflict(CheckoutError):
    """The remote branch has commits that conflict with the sources: `commit` is the
    remote branch's tip, `files` the conflicting paths in the repository and `changes`
    what those commits changed in them, as a diff from where the histories parted."""

    def __init__(self, message: str, branch: str, commit: str, files: list[str],
                 changes: str):
        super().__init__(message)
        self.branch = branch
        self.commit = commit
        self.files = files
        self.changes = changes


def _author(author: tuple[str, str]) -> list[str]:
    return ['-c', f'user.name={author[0]}', '-c', f'user.email={author[1]}']


async def _merge_remote(target: Path, token: str, branch: str, author: tuple[str, str],
                        resolved: str | None) -> None:
    """Merge the commits of the remote `branch` the working copy does not have into it.

    When the remote tip is `resolved`, commits whose changes the sources were already
    made to take in, the conflicting files are taken as the sources have them. Otherwise
    a conflict raises PushConflict, and anything else that stops the merge CheckoutError,
    with the working copy as it was."""
    # The shallow clone lacks the history a merge base is found in.
    shallow = await _git('rev-parse', '--is-shallow-repository', cwd=target) == 'true'
    await _git('fetch', '--quiet', '--no-tags', *(['--unshallow'] if shallow else []),
               'origin', f'refs/heads/{branch}', cwd=target, token=token)
    remote = await _git('rev-parse', 'FETCH_HEAD', cwd=target)
    try:
        await _git(*_author(author), 'merge', '--quiet', '--no-edit', '--no-verify', '-m',
                   f'Merge the commits pushed to {branch} meanwhile', 'FETCH_HEAD',
                   cwd=target)
        return
    except CheckoutError as exc:
        failure = exc
    conflicts = [path for path in (await _git(
        'diff', '--name-only', '-z', '--diff-filter=U', cwd=target)).split('\0') if path]
    if conflicts and resolved == remote:
        for path in conflicts:
            try:
                await _git('cat-file', '-e', f'HEAD:{path}', cwd=target)
            except CheckoutError:
                await _git('rm', '--quiet', '--', path, cwd=target)
            else:
                await _git('checkout', 'HEAD', '--', path, cwd=target)
        await _git(*_author(author), 'commit', '--quiet', '--no-edit', '--no-verify',
                   cwd=target)
        return
    changes = ''
    if conflicts:
        base = await _git('merge-base', 'HEAD', 'FETCH_HEAD', cwd=target)
        changes = await _git('diff', '--no-color', '--no-ext-diff', base, 'FETCH_HEAD',
                             '--', *conflicts, cwd=target)
    try:
        await _git('merge', '--abort', cwd=target)
    except CheckoutError:
        pass  # Nothing was merged, such as with unrelated histories.
    if not conflicts:
        raise CheckoutError(
            f'The branch {branch} on GitHub has commits this session did not push, and they '
            f'cannot be merged: {str(failure)[-500:]}. Delete that branch on GitHub, then '
            'retry the push.') from failure
    named = ', '.join(conflicts[:LISTED_IGNORED]) + (
        f' and {len(conflicts) - LISTED_IGNORED} more files'
        if len(conflicts) > LISTED_IGNORED else '')
    raise PushConflict(
        f'The branch {branch} on GitHub has commits this session did not push, and they '
        f'conflict with the sources in {named}.', branch, remote, conflicts,
        changes if len(changes) <= CONFLICT_CHARS
        else changes[:CONFLICT_CHARS] + '\n… (the diff goes on)') from failure


def replace_files(source: Path, target: Path, include: Callable[[Path], bool]) -> None:
    """Make the included files under `target` exactly those of `source`.

    Files the filter excludes were never shown to the author and stay as they are;
    included files the author removed are deleted. Executable files stay executable.
    """
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    for item in sorted(target.rglob('*')):
        relative = item.relative_to(target)
        if (relative.parts[0] != '.git' and include(relative)
                and (item.is_file() or item.is_symlink())
                and not (source / relative).is_file()):
            item.unlink()
    for item in sorted(source.rglob('*')):
        relative = item.relative_to(source)
        if item.is_symlink() or not item.is_file() or not include(relative):
            continue
        destination = target / relative
        if relative.parts[0] == '.git' or not destination.resolve().is_relative_to(root):
            raise CheckoutError('Generated files must stay inside the working copy')
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy_file(item, destination)


async def push(target: Path, repository: RepositorySelection, token: str, *, branch: str,
               message: str, author: tuple[str, str], verified: bool = True,
               resolved: str | None = None) -> RepositoryPublication:
    """Commit the working copy to `branch` and push it. Safe to repeat after a failure.

    Never forces: when the remote branch has commits the working copy does not, such as
    a merge of the base branch made on GitHub or an earlier push of the session from a
    working copy since cloned again, they are merged in and the merge is pushed. Commits
    that conflict with the sources raise PushConflict and leave the remote branch as it
    is, unless the remote tip is `resolved` (see _merge_remote).

    The repository's .gitignore is respected, so build output left by the authors stays
    out of the branch. The working copy started as a fresh clone, so whatever is ignored
    now came from the verified sources; those paths are reported as `ignored` rather
    than dropped silently, in case a rule matches a real source file."""
    validate_selection(repository['full_name'], branch)
    if not await run_sync((target / '.git').is_dir):
        raise CheckoutError('The repository checkout is unavailable')
    await _git('checkout', '--quiet', '-B', branch, cwd=target)
    await _git('add', '--all', cwd=target)
    # Ignored directories are listed once, not file by file.
    ignored = [line[3:] for line in (await _git(
        'status', '--porcelain', '--ignored', cwd=target)).splitlines()
        if line.startswith('!! ')]
    if await _git('status', '--porcelain', cwd=target):
        await _git('-c', f'user.name={author[0]}', '-c', f'user.email={author[1]}',
                   'commit', '--quiet', '--no-verify', '-m', message, cwd=target)
    try:
        await _git('push', '--quiet', 'origin', f'HEAD:refs/heads/{branch}', cwd=target,
                   token=token)
    except CheckoutError as exc:
        if '[rejected]' not in str(exc):
            raise
        await _merge_remote(target, token, branch, author, resolved)
        await _git('push', '--quiet', 'origin', f'HEAD:refs/heads/{branch}', cwd=target,
                   token=token)
    commit = await _git('rev-parse', 'HEAD', cwd=target)
    return {'branch': branch, 'commit': commit,
            'url': f'{repository["html_url"]}/tree/{branch}', 'verified': verified,
            'ignored': ignored[:LISTED_IGNORED], 'ignored_count': len(ignored)}


async def _git_code(*args: str, cwd: Path, token: str | None = None) -> tuple[int, str]:
    """Run git and return its exit code with its output, as _git would report it."""
    try:
        return 0, await _git(*args, cwd=cwd, token=token)
    except CheckoutError as exc:
        return 1, str(exc)


# Where the session's working copy remembers the merge of the base branch its later
# pushes keep: the tree of the sources as the last push wrote them, before any earlier
# merge was laid over them; the last merge of the base branch pushed; and the tree of the
# sources that merge was made over.
SOURCES_REF = 'refs/sdlc/sources'
MERGED_REF = 'refs/sdlc/merged'
MERGED_SOURCES_REF = 'refs/sdlc/merged-sources'
# Combines the files of the contract directory (by name) both sides of a merge changed:
# (base, ours, theirs) -> the combined files, None for a removed one.
ContractMerge = Callable[[dict[str, bytes], dict[str, bytes], dict[str, bytes]],
                         dict[str, bytes | None]]


async def _git_bytes(*args: str, cwd: Path) -> tuple[int, bytes]:
    """Run git without a token and return its exit code with its output as it is."""
    process = await asyncio.create_subprocess_exec(
        'git', *args, cwd=cwd, env=_environment(None), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return process.returncode or 0, output


async def _blob(revision: str, path: str, cwd: Path) -> bytes | None:
    """The file `path` as `revision` (a commit or a tree) has it; None without one."""
    code, data = await _git_bytes('cat-file', 'blob', f'{revision}:{path}', cwd=cwd)
    return data if code == 0 else None


async def _revision(name: str, cwd: Path) -> str | None:
    code, output = await _git_code('rev-parse', '--verify', '--quiet', name, cwd=cwd)
    return output if code == 0 and output else None


def _contract_name(path: str) -> str | None:
    """The name of a file of the contract directory itself, such as requirements.json."""
    prefix = f'{CONTRACTS_DIR}/'
    if not path.startswith(prefix) or '/' in path[len(prefix):]:
        return None
    return path[len(prefix):]


async def _contract_files(revision: str | None, cwd: Path) -> dict[str, bytes]:
    """The files of the contract directory itself as `revision` has them, by name."""
    if revision is None:
        return {}
    code, listing = await _git_bytes('ls-tree', '-z', revision, f'{CONTRACTS_DIR}/', cwd=cwd)
    files: dict[str, bytes] = {}
    if code:
        return files
    for entry in listing.split(b'\0'):
        meta, _, raw = entry.partition(b'\t')
        path = raw.decode('utf-8', 'replace')
        name = _contract_name(path)
        if name is None or meta.split(b' ')[1:2] != [b'blob']:
            continue
        data = await _blob(revision, path, cwd)
        if data is not None:
            files[name] = data
    return files


async def _combined_contract(combine: ContractMerge | None, revisions: tuple[
        str | None, str | None, str | None], cwd: Path) -> dict[str, bytes | None]:
    """The contract files `combine` settles between the three revisions; none when it
    cannot, which leaves them to a merge of their lines."""
    if combine is None:
        return {}
    sides = [await _contract_files(revision, cwd) for revision in revisions]
    try:
        return combine(*sides)
    except Exception:  # noqa: BLE001 - a contract that cannot be combined is merged by line
        return {}


async def _merge_lines(base: bytes | None, ours: bytes, theirs: bytes, *,
                       union: bool) -> bytes:
    """A three-way merge of the lines of a file: both sides' changes, and where they
    touch the same lines both versions (`union`, for prose) or ours."""
    directory = Path(await run_sync(tempfile.mkdtemp))
    try:
        paths = [directory / name for name in ('ours', 'base', 'theirs')]
        for path, data in zip(paths, (ours, base or b'', theirs)):
            await run_sync(path.write_bytes, data)
        code, merged = await _git_bytes(
            'merge-file', '-p', '--union' if union else '--ours',
            *(str(path) for path in paths), cwd=directory)
    finally:
        await run_sync(partial(shutil.rmtree, directory, ignore_errors=True))
    # A negative code (shown as 128 or more) is an error, such as for a binary file.
    return merged if 0 <= code < 128 else ours


async def _three_way(path: str, base: bytes | None, ours: bytes | None,
                     theirs: bytes | None, combined: dict[str, bytes | None]) -> bytes | None:
    """The file `path` with what both sides changed since `base`; None when removed.
    Where both changed it, a contract file is combined by `combined`, or else its lines
    are merged; a file one side removed and the other changed stays as ours has it."""
    if ours == theirs or base == theirs:
        return ours
    if base == ours:
        return theirs
    name = _contract_name(path)
    if name is not None and name in combined:
        return combined[name]
    if ours is None or theirs is None:
        return ours
    return await _merge_lines(base, ours, theirs, union=path.endswith('.md'))


async def _write_file(target: Path, path: str, data: bytes | None) -> None:
    """Write `data` to `path` of the working copy, or remove it for None."""
    destination = target / path
    if not await run_sync(lambda: destination.resolve().is_relative_to(target.resolve())):
        raise CheckoutError('Merged files must stay inside the working copy')
    if data is None:
        await run_sync(partial(destination.unlink, missing_ok=True))
        return
    await run_sync(partial(destination.parent.mkdir, parents=True, exist_ok=True))
    if await run_sync(destination.is_symlink):
        await run_sync(destination.unlink)
    await run_sync(destination.write_bytes, data)


async def keep_base_merge(target: Path, combine: ContractMerge | None = None) -> None:
    """Lay the last merge of the base branch pushed from this working copy (see
    merge_base_branch) over the sources just written into it, before they are pushed:
    the sources were made before that merge, so each file the merge changed from the
    sources it was made over takes what the merge brought in, combined with what the
    sources changed since. Without this a later push would undo the base branch's
    changes. Records the tree of the sources as written, which a later merge is made
    over. A working copy without Git is left as it is."""
    if not await run_sync((target / '.git').is_dir):
        return
    await _git('add', '--all', cwd=target)
    sources = await _git('write-tree', cwd=target)
    merged = await _revision(MERGED_REF, target)
    kept = await _revision(MERGED_SOURCES_REF, target)
    if merged and kept:
        paths = [path for path in (await _git(
            'diff', '--name-only', '-z', '--no-renames', kept, merged, '--',
            cwd=target)).split('\0') if path]
        combined = (await _combined_contract(combine, (kept, sources, merged), target)
                    if any(_contract_name(path) for path in paths) else {})
        for path in paths:
            ours = await _blob(sources, path, target)
            final = await _three_way(path, await _blob(kept, path, target), ours,
                                     await _blob(merged, path, target), combined)
            if final != ours:
                await _write_file(target, path, final)
        await _git('add', '--all', cwd=target)
    await _git('update-ref', SOURCES_REF, sources, cwd=target)


async def _remember_merge(target: Path, merge: str, head: str) -> None:
    """Record `merge` as the last merge of the base branch, made over the sources the
    last push wrote, or else over the branch's tip `head`, for keep_base_merge."""
    sources = (await _revision(SOURCES_REF, target)
               or await _git('rev-parse', f'{head}^{{tree}}', cwd=target))
    await _git('update-ref', MERGED_SOURCES_REF, sources, cwd=target)
    await _git('update-ref', MERGED_REF, merge, cwd=target)


def _changes_text(changes: str) -> str:
    return (changes if len(changes) <= CONFLICT_CHARS
            else changes[:CONFLICT_CHARS] + '\n… (the diff goes on)')


async def base_conflicts(work: Path, full_name: str, token: str, number: int,
                         base: str) -> dict[str, object]:
    """How pull request `number` of `full_name` merges with its base branch `base` as both
    are now on GitHub, worked out in the empty directory `work`: {'conflicting',
    'base_commit' (the base branch's tip), 'files' (the conflicting paths), 'changes'
    (what the base branch changed in them since the histories parted)}. Raises
    CheckoutError when they cannot be read or merged at all."""
    validate_selection(full_name, base)
    await _git('init', '--quiet', '--bare', str(work), cwd=work.parent)
    await _git('fetch', '--quiet', '--no-tags', f'{GITHUB_URL}{full_name}.git',
               f'+refs/heads/{base}:refs/sdlc/base',
               f'+refs/pull/{number}/head:refs/sdlc/head', cwd=work, token=token)
    base_commit = await _git('rev-parse', 'refs/sdlc/base', cwd=work)
    process = await asyncio.create_subprocess_exec(
        'git', 'merge-tree', '--write-tree', '--name-only', '--no-messages', '-z',
        'refs/sdlc/head', 'refs/sdlc/base', cwd=work, env=_environment(None),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        output, errors = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode == 0:
        return {'conflicting': False, 'base_commit': base_commit, 'files': [], 'changes': ''}
    if process.returncode != 1:
        text = errors.decode('utf-8', 'replace').strip()
        raise CheckoutError(f'git merge-tree failed: {text[-OUTPUT_CHARS:]}')
    # The tree written, then each conflicting path once.
    entries = [item for item in output.decode('utf-8', 'replace').split('\0') if item]
    files = list(dict.fromkeys(entries[1:]))
    changes = ''
    if files:
        merge_base = await _git('merge-base', 'refs/sdlc/head', 'refs/sdlc/base', cwd=work)
        changes = await _git('diff', '--no-color', '--no-ext-diff', merge_base,
                             'refs/sdlc/base', '--', *files, cwd=work)
    return {'conflicting': True, 'base_commit': base_commit, 'files': files,
            'changes': _changes_text(changes)}


async def merge_base_branch(target: Path, repository: RepositorySelection, token: str, *,
                            branches: list[str], base_commit: str,
                            resolve: Callable[[str], Path | None], message: str,
                            author: tuple[str, str], verified: bool = True,
                            combine: ContractMerge | None = None) -> RepositoryPublication:
    """Merge `base_commit`, the base branch's tip, into the pull request branch on GitHub,
    the first of `branches` that exists there, and push the merge commit, whose parents
    are the branch's tip and `base_commit`. Each conflicting path of the contract
    (contracts/) combines both sides: the files `combine` settles, and any other with
    the lines of both merged. Any other conflicting path is taken from the file
    `resolve(path)` names, as the fix round left it, or else as the branch has it. A
    branch already holding `base_commit` is left as it is. Never forces: a branch that
    moved meanwhile fails the push with CheckoutError. The merge is remembered in the
    working copy, so that later pushes of the sources keep it (see keep_base_merge)."""
    if not re.fullmatch(r'[0-9a-f]{40,64}', base_commit):
        raise CheckoutError('Invalid base commit')
    if not await run_sync((target / '.git').is_dir):
        raise CheckoutError('The repository checkout is unavailable')
    shallow = await _git('rev-parse', '--is-shallow-repository', cwd=target) == 'true'
    branch = None
    for candidate in branches:
        validate_selection(repository['full_name'], candidate)
        code, _ = await _git_code(
            'fetch', '--quiet', '--no-tags', *(['--unshallow'] if shallow else []),
            'origin', f'+refs/heads/{candidate}:refs/remotes/origin/{candidate}',
            cwd=target, token=token)
        if code == 0:
            branch = candidate
            break
    if branch is None:
        raise CheckoutError('The pull request branch was not found on GitHub.')
    head = await _git('rev-parse', f'refs/remotes/origin/{branch}', cwd=target)
    await _git('fetch', '--quiet', '--no-tags', 'origin', base_commit, cwd=target,
               token=token)
    # The working copy is the session's own: what it holds is written again from the
    # sources by every push, so it starts from the branch as GitHub has it.
    await _git('reset', '--quiet', '--hard', cwd=target)
    await _git('clean', '--quiet', '-fd', cwd=target)
    await _git('checkout', '--quiet', '-f', '-B', branch, head, cwd=target)
    url = f'{repository["html_url"]}/tree/{branch}'
    code, _ = await _git_code('merge-base', '--is-ancestor', base_commit, 'HEAD', cwd=target)
    if code == 0:
        # The base branch was merged already, such as by hand.
        await _remember_merge(target, head, head)
        return {'branch': branch, 'commit': head, 'url': url, 'verified': verified,
                'ignored': [], 'ignored_count': 0}
    try:
        commit = await _merge_and_push(target, token, branch=branch,
                                       base_commit=base_commit, resolve=resolve,
                                       message=message, author=author, combine=combine)
    except BaseException:
        # Nothing of a merge that was not pushed may stay in the working copy: a later
        # push of the sources would carry it and so undo the base branch's changes.
        await _restore_branch(target, head)
        raise
    await _remember_merge(target, commit, head)
    return {'branch': branch, 'commit': commit, 'url': url, 'verified': verified,
            'ignored': [], 'ignored_count': 0}


async def _restore_branch(target: Path, head: str) -> None:
    """Abort any merge in progress and put the checked-out branch, its index and its
    files back at `head`, the tip it was checked out from. Best effort: it runs while
    another failure is raised, which it must not hide."""
    try:
        if await _revision('MERGE_HEAD', target):
            await _git_code('merge', '--abort', cwd=target)
        await _git_code('reset', '--quiet', '--hard', head, cwd=target)
        await _git_code('clean', '--quiet', '-fd', cwd=target)
    except Exception:  # noqa: BLE001 - the original failure is the one to report
        pass


async def _merge_and_push(target: Path, token: str, *, branch: str, base_commit: str,
                          resolve: Callable[[str], Path | None], message: str,
                          author: tuple[str, str],
                          combine: ContractMerge | None) -> str:
    """Merge `base_commit` into the checked-out `branch`, settle its conflicts as
    merge_base_branch describes, commit and push the merge; returns the merge commit."""
    code, failure = await _git_code(*_author(author), 'merge', '--quiet', '--no-ff',
                                    '--no-commit', '--no-verify', base_commit, cwd=target)
    conflicts = [path for path in (await _git(
        'diff', '--name-only', '-z', '--diff-filter=U', cwd=target)).split('\0') if path]
    if code and not conflicts:
        raise CheckoutError(f'The base branch cannot be merged: {failure[-500:]}')
    root = target.resolve()
    contract = [path for path in conflicts if path.startswith(f'{CONTRACTS_DIR}/')]
    if contract:
        # The contract keeps what the delivery wrote and takes in what the base branch
        # changed, such as the requirements of another delivery merged into it.
        code, merge_base = await _git_code('merge-base', 'HEAD', base_commit, cwd=target)
        old = merge_base if code == 0 and merge_base else None
        combined = (await _combined_contract(combine, (old, 'HEAD', base_commit), target)
                    if any(_contract_name(path) for path in contract) else {})
        for path in contract:
            final = await _three_way(
                path, await _blob(old, path, target) if old else None,
                await _blob('HEAD', path, target), await _blob(base_commit, path, target),
                combined)
            await _write_file(target, path, final)
            if final is None:
                await _git('rm', '--quiet', '--force', '--ignore-unmatch', '--', path,
                           cwd=target)
            else:
                await _git('add', '--', path, cwd=target)
        # The documents rendered from a combined list follow it, even where their own
        # lines merged cleanly.
        for name, data in combined.items():
            path = f'{CONTRACTS_DIR}/{name}'
            if path in contract:
                continue
            await _write_file(target, path, data)
            await _git('add', '--all', '--', path, cwd=target)
    for path in conflicts:
        if path.startswith(f'{CONTRACTS_DIR}/'):
            continue
        source = resolve(path)
        destination = target / path
        if (source is not None and await run_sync(source.is_file)
                and destination.resolve().is_relative_to(root)):
            await run_sync(destination.parent.mkdir, parents=True, exist_ok=True)
            await run_sync(copy_file, source, destination)
            await _git('add', '--', path, cwd=target)
            continue
        # As the pull request branch has it, or removed where the branch removed it.
        code, _ = await _git_code('cat-file', '-e', f'HEAD:{path}', cwd=target)
        if code:
            await _git('rm', '--quiet', '--force', '--', path, cwd=target)
        else:
            await _git('checkout', 'HEAD', '--', path, cwd=target)
            await _git('add', '--', path, cwd=target)
    await _git(*_author(author), 'commit', '--quiet', '--no-verify', '-m', message,
               cwd=target)
    commit = await _git('rev-parse', 'HEAD', cwd=target)
    await _git('push', '--quiet', 'origin', f'HEAD:refs/heads/{branch}', cwd=target,
               token=token)
    return commit
