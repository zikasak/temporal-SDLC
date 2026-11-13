"""The work of a session's authors, kept in its repository as it is written.

Every stage that changes the project (the implementation, the tests, the documentation)
commits what its author left, after each run and whatever became of it, to a hidden ref of
the repository: `refs/sdlc/<session-id>/<stage>/latest`, and the output it published to
`refs/sdlc/<session-id>/<stage>/published`. Such refs are no branch: GitHub shows them in
no branch list, runs no CI for them and they never touch the pull request. They keep the
work when the worker's disk does not, such as after a redeploy, and a stage whose
published output is missing restores it from there.

The commit is made without the LLM, so it is made when the LLM failed; the LLM only writes
its message when it can, else a message is composed from the change itself. Saving never
fails a stage: what could not be saved is logged.

The session's checkout is left as it is: the commit is built in an index and a tree of its
own, on top of the commit the session started from."""

import asyncio
import json
import logging
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.asyncio_utils import async_context, run_sync
from app.github.checkout import (
    CHECKOUT_DIR, MANIFEST, CheckoutError, _git, replace_files,
)

logger = logging.getLogger(__name__)

REF_ROOT = 'refs/sdlc'
LATEST, PUBLISHED = 'latest', 'published'
# The commits saved last by this worker, by ref, to chain the next one on.
RECORD = 'work-refs.json'
# Bounds of what a commit message is written from, and of the message.
DIFF_CHARS = 20000
SUBJECT_CHARS = 72
BODY_CHARS = 2000
SAVE_TIMEOUT = 300
STATUS_TRAILER = 'Sdlc-Status'
_NAME = re.compile(r'[A-Za-z0-9_-]{1,200}')

# Writes a commit message from (stage, status, diff); None leaves it to the fallback.
MessageWriter = Callable[[str, str, str], Awaitable[str | None]]


def work_ref(session_id: str, stage: str, kind: str = LATEST) -> str:
    if not (_NAME.fullmatch(session_id) and _NAME.fullmatch(stage)
            and kind in (LATEST, PUBLISHED)):
        raise CheckoutError('Invalid work reference')
    return f'{REF_ROOT}/{session_id}/{stage}/{kind}'


def _manifest(session_dir: Path) -> dict[str, Any] | None:
    try:
        manifest = json.loads((session_dir / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    if (not isinstance(manifest, dict) or not isinstance(manifest.get('full_name'), str)
            or not re.fullmatch(r'[0-9a-f]{40,64}', str(manifest.get('commit')))):
        return None
    return manifest


def _record(session_dir: Path) -> dict[str, str]:
    try:
        record = json.loads((session_dir / RECORD).read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _remember(session_dir: Path, ref: str, commit: str) -> None:
    record = {**_record(session_dir), ref: commit}
    temporary = session_dir / f'.{RECORD}.tmp'
    temporary.write_text(json.dumps(record))
    temporary.replace(session_dir / RECORD)


def fallback_message(stage: str, status: str, stat: str) -> str:
    """A commit message composed from the change alone."""
    files = [line.split('|')[0].strip() for line in stat.splitlines() if '|' in line]
    subject = f'WIP {stage}: {status}'
    if files:
        named = ', '.join(Path(item).name for item in files[:3])
        more = f' and {len(files) - 3} more' if len(files) > 3 else ''
        subject = f'{subject} ({named}{more})'
    return f'{subject[:SUBJECT_CHARS]}\n\n{stat[-BODY_CHARS:]}'.strip()


def _clean_message(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r'^```\w*\n|\n?```$', '', text.strip()).strip()
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        return None
    subject = lines[0].strip().strip('"')[:SUBJECT_CHARS]
    body = '\n'.join(lines[1:]).strip()[:BODY_CHARS]
    return f'{subject}\n\n{body}'.strip()


async def save_work(session_dir: Path, session_id: str, stage: str, tree: Path,
                    include: Callable[[Path], bool], *, prefix: str = '', status: str,
                    published: bool = False, token: Callable[[], Awaitable[tuple[str, str]]],
                    write_message: MessageWriter | None = None) -> str | None:
    """Commit `tree`, the files of the project `include` selects, placed at `prefix` of
    the repository as the session started from it, to the stage's `latest` work ref, and to
    its `published` one too when it is the stage's published output; push them. `token`
    gives the GitHub token and the commit author's name and e-mail joined by a newline.
    Returns the commit, or None when the session has no repository or nothing was saved."""
    if not await run_sync(tree.is_dir):
        return None
    manifest = await run_sync(_manifest, session_dir)
    checkout = session_dir / CHECKOUT_DIR
    if manifest is None or not await run_sync((checkout / '.git').is_dir):
        return None
    try:
        async with asyncio.timeout(SAVE_TIMEOUT):
            return await _save(session_dir, session_id, stage, tree, include, prefix, status,
                               published, manifest, token, write_message)
    except Exception as exc:
        logger.warning('The %s work of session %s was not saved to its repository: %s',
                       stage, session_id, exc)
        return None


async def _save(session_dir: Path, session_id: str, stage: str, tree: Path,
                include: Callable[[Path], bool], prefix: str, status: str, published: bool,
                manifest: dict[str, Any], token: Callable[[], Awaitable[tuple[str, str]]],
                write_message: MessageWriter | None) -> str:
    checkout = session_dir / CHECKOUT_DIR
    base = manifest['commit']
    latest = work_ref(session_id, stage)
    parent = _record(session_dir).get(latest) or base
    async with async_context(tempfile.TemporaryDirectory, dir=session_dir) as temporary:
        work = Path(temporary) / 'tree'
        env = {'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
        try:
            await _git('cat-file', '-e', f'{parent}^{{commit}}', cwd=checkout)
        except CheckoutError:
            # The commit this worker saved last was made elsewhere or is gone.
            parent = base
        await _git('read-tree', base, cwd=checkout, env=env)
        await _git('checkout-index', '--all', f'--prefix={work}/', cwd=checkout, env=env)
        target = work / prefix if prefix else work
        await run_sync(replace_files, tree, target, include)
        # Run inside the tree, so that its own .gitignore files apply to what is added.
        await _git('add', '--all', '.', cwd=work, env={
            **env, 'GIT_DIR': str((checkout / '.git').resolve()), 'GIT_WORK_TREE': str(work)})
        written = await _git('write-tree', cwd=checkout, env=env)
        if written == await _git('rev-parse', f'{parent}^{{tree}}', cwd=checkout):
            commit = parent
        else:
            stat = await _git('diff', '--no-color', '--stat=120', parent, written,
                              cwd=checkout)
            message = None
            if write_message is not None:
                patch = await _git('diff', '--no-color', '--unified=1', parent, written,
                                   cwd=checkout)
                try:
                    message = _clean_message(await write_message(
                        stage, status, f'{stat}\n\n{patch}'[:DIFF_CHARS]))
                except Exception as exc:
                    logger.info('No LLM commit message for the %s work of %s: %s', stage,
                                session_id, type(exc).__name__)
            message = message or fallback_message(stage, status, stat)
            message += f'\n\nSession: {session_id}\n{STATUS_TRAILER}: {status}'
            secret, author = await token()
            name, _, email = author.partition('\n')
            identity = {'GIT_AUTHOR_NAME': name, 'GIT_AUTHOR_EMAIL': email,
                        'GIT_COMMITTER_NAME': name, 'GIT_COMMITTER_EMAIL': email}
            commit = await _git('commit-tree', written, '-p', parent, '-m', message,
                                cwd=checkout, env=identity)
        refs = [latest, *([work_ref(session_id, stage, PUBLISHED)] if published else [])]
        secret, _ = await token()
        await _git('push', '--quiet', '--force', 'origin',
                   *(f'{commit}:{ref}' for ref in refs), cwd=checkout, token=secret)
    for ref in refs:
        await run_sync(_remember, session_dir, ref, commit)
    logger.info('Saved the %s work of session %s (%s) as %s', stage, session_id, status,
                commit[:12])
    return commit


async def base_files(session_dir: Path, target: Path) -> bool:
    """Write the files of the commit the session started from to the missing directory
    `target`, from its checkout's history: what the checkout held before the delivery
    pushed to it. False when that commit is unknown or not in the checkout."""
    manifest = await run_sync(_manifest, session_dir)
    checkout = session_dir / CHECKOUT_DIR
    if manifest is None or not await run_sync((checkout / '.git').is_dir):
        return False
    async with async_context(tempfile.TemporaryDirectory, dir=session_dir) as temporary:
        env = {'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
        try:
            await _git('read-tree', manifest['commit'], cwd=checkout, env=env)
            await _git('checkout-index', '--all', f'--prefix={target}/', cwd=checkout, env=env)
        except CheckoutError as exc:
            logger.warning('The files of the commit session %s started from are unavailable: '
                           '%s', session_dir.name, exc)
            return False
    return True


async def checkout_moved(session_dir: Path) -> bool:
    """Whether the session's checkout no longer holds the commit it started from, such as
    after the delivery pushed to it."""
    manifest = await run_sync(_manifest, session_dir)
    checkout = session_dir / CHECKOUT_DIR
    if manifest is None or not await run_sync((checkout / '.git').is_dir):
        return False
    try:
        head = await _git('rev-parse', 'HEAD', cwd=checkout)
        changed = await _git('status', '--porcelain', cwd=checkout)
    except CheckoutError:
        return True
    return head != manifest['commit'] or bool(changed)


async def restore_work(session_dir: Path, session_id: str, stage: str, target: Path,
                       include: Callable[[Path], bool], *, prefix: str = '',
                       token: Callable[[], Awaitable[tuple[str, str]]],
                       kind: str = PUBLISHED) -> bool:
    """Write the stage's published output, as its `published` work ref holds it, or with
    `kind` LATEST what its last run left, to the missing directory `target`: the files
    `include` selects under `prefix`. False when it could not be restored, such as when
    nothing was saved there."""
    manifest = await run_sync(_manifest, session_dir)
    checkout = session_dir / CHECKOUT_DIR
    if (manifest is None or await run_sync(target.exists)
            or not await run_sync((checkout / '.git').is_dir)):
        return False
    ref = work_ref(session_id, stage, kind)
    try:
        secret, _ = await token()
        await _git('fetch', '--quiet', '--no-tags', '--depth', '1', 'origin',
                   f'+{ref}:{ref}', cwd=checkout, token=secret)
        await run_sync(target.parent.mkdir, parents=True, exist_ok=True)
        async with async_context(tempfile.TemporaryDirectory, dir=target.parent) as temporary:
            work = Path(temporary) / 'tree'
            env = {'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
            await _git('read-tree', ref, cwd=checkout, env=env)
            await _git('checkout-index', '--all', f'--prefix={work}/', cwd=checkout, env=env)
            source = work / prefix if prefix else work
            clean = Path(temporary) / 'clean'
            await run_sync(clean.mkdir)
            if await run_sync(source.is_dir):
                await run_sync(replace_files, source, clean, include)
            await run_sync(shutil.move, str(clean), str(target))
    except (CheckoutError, OSError) as exc:
        logger.info('The %s output of session %s could not be restored from %s: %s',
                       stage, session_id, ref, exc)
        return False
    logger.info('Restored the %s output of session %s from %s', stage, session_id, ref)
    return True
