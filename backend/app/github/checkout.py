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
from pathlib import Path
from typing import Any

from app.asyncio_utils import async_context, run_sync
from app.agent_workflows.host import copy_file
from app.github import contract_merge
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


async def current_branch(target: Path) -> str | None:
    """The branch the working copy has checked out; None when detached or unavailable."""
    if not await run_sync((target / '.git').is_dir):
        return None
    try:
        name = await _git('symbolic-ref', '--quiet', '--short', 'HEAD', cwd=target)
    except CheckoutError:
        return None
    return name or None


async def remote_branch_exists(target: Path, token: str, branch: str) -> bool:
    """Whether the repository on GitHub has `branch`."""
    found = await _git('ls-remote', '--heads', 'origin', f'refs/heads/{branch}', cwd=target,
                       token=token)
    return bool(found.strip())


async def _git_bytes(*args: str, cwd: Path) -> bytes | None:
    """The raw output of a local git command; None when it fails."""
    process = await asyncio.create_subprocess_exec(
        'git', *args, cwd=cwd, env=_environment(None), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return None if process.returncode else output


async def _union_merge(target: Path, base: str, ours: str, theirs: str) -> str:
    """The merge of three texts that keeps the lines both sides added."""
    with tempfile.TemporaryDirectory() as directory:
        paths = []
        for name, text in (('ours', ours), ('base', base), ('theirs', theirs)):
            path = Path(directory) / name
            await run_sync(path.write_text, text)
            paths.append(str(path))
        merged = await _git_bytes('merge-file', '--union', '-p', *paths, cwd=target)
    if merged is None:
        raise CheckoutError('The contract files could not be merged.')
    return merged.decode('utf-8', 'replace')


async def _merge_contract_files(target: Path, base_commit: str,
                                conflicts: list[str]) -> set[str]:
    """Settle the conflicting files of the repository's contract (contracts/, without
    its component and part contracts) in a merge of `base_commit` into HEAD by merging
    both sides' contracts by their content, and write the files rendered from them; the
    conflicting paths settled."""
    names = {path.split('/', 1)[1] for path in conflicts
             if path.startswith(f'{CONTRACTS_DIR}/') and path.count('/') == 1}
    if not names:
        return set()
    parted = await _git_bytes('merge-base', 'HEAD', base_commit, cwd=target)
    revisions = (parted.decode().strip() if parted else None, 'HEAD', base_commit)
    versions: dict[str, contract_merge.Versions] = {}
    for name in {*names, *contract_merge.CONTRACT_FILES}:
        texts: list[str | None] = []
        for revision in revisions:
            shown = (await _git_bytes('show', f'{revision}:{CONTRACTS_DIR}/{name}', cwd=target)
                     if revision else None)
            texts.append(None if shown is None else shown.decode('utf-8', 'replace'))
        versions[name] = (texts[0], texts[1], texts[2])

    async def merge_text(base: str, ours: str, theirs: str) -> str:
        return await _union_merge(target, base, ours, theirs)

    merged = await contract_merge.merge_contract(versions, names, merge_text)
    directory = target / CONTRACTS_DIR
    for name, text in merged.items():
        path = f'{CONTRACTS_DIR}/{name}'
        if text is None:
            await _git('rm', '--quiet', '--force', '--ignore-unmatch', '--', path,
                       cwd=target)
            continue
        await run_sync(directory.mkdir, exist_ok=True)
        await run_sync((directory / name).write_text, text)
        await _git('add', '--', path, cwd=target)
    return {f'{CONTRACTS_DIR}/{name}' for name in names if name in merged}


async def merge_base_commit(target: Path, repository: RepositorySelection, token: str, *,
                            branch: str, base_commit: str, message: str,
                            author: tuple[str, str],
                            verified: bool = True) -> RepositoryPublication:
    """Merge `base_commit`, the tip of a pull request's base branch, into the remote
    `branch` and push the merge, never forcing. The merge commit has the branch's tip and
    `base_commit` as parents; files that conflict are taken as the branch has them, as a
    fix round made them take in the base branch's changes, except the contract's own
    (contracts/), which are merged by their content so that both sides' requirements,
    API and architecture are kept. Raises CheckoutError when the
    merge cannot be made or pushed, such as when the branch moved meanwhile."""
    validate_selection(repository['full_name'], branch)
    if not re.fullmatch(r'[0-9a-f]{40,64}', base_commit):
        raise CheckoutError('Invalid commit')
    if not await run_sync((target / '.git').is_dir):
        raise CheckoutError('The repository checkout is unavailable')
    shallow = await _git('rev-parse', '--is-shallow-repository', cwd=target) == 'true'
    await _git('fetch', '--quiet', '--no-tags', *(['--unshallow'] if shallow else []),
               'origin', f'refs/heads/{branch}', cwd=target, token=token)
    tip = await _git('rev-parse', 'FETCH_HEAD', cwd=target)
    await _git('checkout', '--quiet', '--force', '-B', branch, tip, cwd=target)
    try:
        await _git('fetch', '--quiet', '--no-tags', 'origin', base_commit, cwd=target,
                   token=token)
    except CheckoutError:
        # Fetching by commit may be refused; the selected branch then brings it.
        await _git('fetch', '--quiet', '--no-tags', 'origin',
                   f'refs/heads/{repository["branch"]}', cwd=target, token=token)
    try:
        await _git('cat-file', '-e', f'{base_commit}^{{commit}}', cwd=target)
    except CheckoutError as exc:
        raise CheckoutError(
            f'The base branch commit {base_commit[:12]} could not be fetched.') from exc
    try:
        await _git(*_author(author), 'merge', '--quiet', '--no-ff', '--no-edit',
                   '--no-verify', '-m', message, base_commit, cwd=target)
    except CheckoutError as exc:
        conflicts = [path for path in (await _git(
            'diff', '--name-only', '-z', '--diff-filter=U', cwd=target)).split('\0') if path]
        if not conflicts:
            try:
                await _git('merge', '--abort', cwd=target)
            except CheckoutError:
                pass  # Nothing was merged.
            raise CheckoutError(
                f'The base branch could not be merged into {branch}: '
                f'{str(exc)[-500:]}') from exc
        # The contract is merged by its content, so the base branch's requirements and
        # API changes are kept beside the delivery's.
        settled = await _merge_contract_files(target, base_commit, conflicts)
        for path in conflicts:
            if path in settled:
                continue
            try:
                await _git('cat-file', '-e', f'HEAD:{path}', cwd=target)
            except CheckoutError:
                await _git('rm', '--quiet', '--', path, cwd=target)
            else:
                await _git('checkout', 'HEAD', '--', path, cwd=target)
        await _git(*_author(author), 'commit', '--quiet', '--no-edit', '--no-verify',
                   cwd=target)
    try:
        await _git('push', '--quiet', 'origin', f'HEAD:refs/heads/{branch}', cwd=target,
                   token=token)
    except CheckoutError as exc:
        if '[rejected]' in str(exc):
            raise CheckoutError(
                f'The branch {branch} on GitHub moved while the base branch was merged '
                'into it.') from exc
        raise
    commit = await _git('rev-parse', 'HEAD', cwd=target)
    return {'branch': branch, 'commit': commit,
            'url': f'{repository["html_url"]}/tree/{branch}', 'verified': verified,
            'ignored': [], 'ignored_count': 0}


async def merge_changes(target: Path) -> dict[str, Any]:
    """What the merge commit at HEAD of the working copy took in from the branch it
    merged: {'parent': the commit it was merged into (its first parent), 'parent_files':
    every path of that commit, 'changed': the paths the merge changed relative to it,
    'contract': {name: text} of the top-level files of contracts/ in that commit}."""
    parent = (await _git('rev-parse', 'HEAD^1', cwd=target)).strip()
    listed = await _git_bytes('ls-tree', '-r', '--name-only', '-z', parent, cwd=target)
    changed = await _git_bytes('diff', '--name-only', '--no-renames', '-z', parent, 'HEAD',
                               cwd=target)
    if listed is None or changed is None:
        raise CheckoutError('The changes of the merge could not be read.')
    parent_files = [item for item in listed.decode('utf-8', 'replace').split('\0') if item]
    contract: dict[str, str] = {}
    for path in parent_files:
        if path.startswith(f'{CONTRACTS_DIR}/') and path.count('/') == 1:
            shown = await _git_bytes('show', f'{parent}:{path}', cwd=target)
            if shown is not None:
                contract[path.split('/', 1)[1]] = shown.decode('utf-8', 'replace')
    return {'parent': parent, 'parent_files': parent_files,
            'changed': [item for item in changed.decode('utf-8', 'replace').split('\0')
                        if item],
            'contract': contract}


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
