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
from collections.abc import Callable
from pathlib import Path

from app.agent_workflows.host import copy_file
from app.github.client import validate_selection
from app.github.models import RepositoryCheckout, RepositoryPublication, RepositorySelection

# Beside a session's generated artifacts.
CHECKOUT_DIR = 'repository'
MANIFEST = 'repository.json'
# The independent contract tests' directory inside the repository.
TESTS_DIR = 'contract-tests'
BRANCH_PREFIX = 'generated/'
GITHUB_URL = 'https://github.com/'
GIT_TIMEOUT = 540
OUTPUT_CHARS = 1500
LISTED_IGNORED = 20


class CheckoutError(RuntimeError):
    pass


def checkout_path(base: Path, session_id: str) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise CheckoutError('Invalid session ID')
    return base / session_id / CHECKOUT_DIR


def publication_branch(session_id: str) -> str:
    return BRANCH_PREFIX + session_id.removeprefix('api-design-session-')[:12]


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


async def _git(*args: str, cwd: Path, token: str | None = None) -> str:
    process = await asyncio.create_subprocess_exec(
        'git', *args, cwd=cwd, env=_environment(token), stdin=asyncio.subprocess.DEVNULL,
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
        text = text.replace(token, '[redacted]').replace(_credentials(token), '[redacted]')
    if process.returncode:
        raise CheckoutError(f'git {args[0]} failed: {text[-OUTPUT_CHARS:]}')
    return text


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
                   token: str) -> RepositoryCheckout:
    """Clone the selected branch into the session, once: a repeated call (an activity
    retry, a restarted run) keeps the existing working copy and whatever it holds."""
    validate_selection(repository['full_name'], repository['branch'])
    recorded = _recorded(session_dir, repository)
    if recorded:
        return recorded
    session_dir.mkdir(parents=True, exist_ok=True)
    target = session_dir / CHECKOUT_DIR
    with tempfile.TemporaryDirectory(dir=session_dir) as temporary:
        clone = Path(temporary) / CHECKOUT_DIR
        await _git(
            'clone', '--quiet', '--single-branch', '--no-tags', '--depth', '1',
            '--config', 'core.symlinks=false', '--branch', repository['branch'], '--',
            f'{GITHUB_URL}{repository["full_name"]}.git', str(clone),
            cwd=session_dir, token=token,
        )
        commit = await _git('rev-parse', 'HEAD', cwd=clone)
        if target.exists():
            shutil.rmtree(target)
        os.replace(clone, target)
        manifest = Path(temporary) / MANIFEST
        manifest.write_text(json.dumps({'full_name': repository['full_name'],
                                        'branch': repository['branch'], 'commit': commit}))
        os.replace(manifest, session_dir / MANIFEST)
    return {'commit': commit}


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
               message: str, author: tuple[str, str]) -> RepositoryPublication:
    """Commit the working copy to `branch` and push it. Never forces: a remote branch
    of that name with other history is an error. Safe to repeat after a failure.

    The repository's .gitignore is respected, so build output left by the authors stays
    out of the branch. The working copy started as a fresh clone, so whatever is ignored
    now came from the verified sources; those paths are reported as `ignored` rather
    than dropped silently, in case a rule matches a real source file."""
    validate_selection(repository['full_name'], branch)
    if not (target / '.git').is_dir():
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
    commit = await _git('rev-parse', 'HEAD', cwd=target)
    await _git('push', '--quiet', 'origin', f'HEAD:refs/heads/{branch}', cwd=target, token=token)
    return {'branch': branch, 'commit': commit,
            'url': f'{repository["html_url"]}/tree/{branch}',
            'ignored': ignored[:LISTED_IGNORED], 'ignored_count': len(ignored)}
