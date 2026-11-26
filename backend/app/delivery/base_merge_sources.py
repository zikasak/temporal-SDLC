"""Keep a session's sources in step with a base branch merge pushed to its pull request.

A base merge fix round pushes a merge of the base branch's tip into the pull request
branch, made in the session's working copy (`repository/`). Every later push of the
session is built from its other sources: the service, the contract tests, the component
tests and the documentation its authors wrote, and the contract the workflow writes. So
that such a push does not take the base branch's changes out again, the merge's changes
are copied into those sources (`refresh_sources`), and the contract the merge combined is
recorded and kept by the later pushes (`record_merged_contract`, `keep_merged_contract`).
"""
import json
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from app.agent_workflows.host import copy_file
from app.delivery.workspace import SessionWorkspace
from app.documentation.activities import SUFFIXES as DOCUMENTATION_SUFFIXES
from app.github.checkout import (BROWSER_TESTS_DIR, COMPONENT_TESTS_DIR, CONTRACTS_DIR,
                                 TESTS_DIR)
from app.implementation.activities import EXCLUDED
from app.languages.composite import BROWSER_MEMBER, PARTS_DIR

# Where the workspace records the contract of its last base merge.
RECORD_DIR = 'base-merge'
CONTRACT_RECORD = 'contract.json'

# Maps a repository path to its place in a source tree, or None when the tree lacks it.
Mapping = Callable[[PurePosixPath], PurePosixPath | None]


def _outside_sources(path: PurePosixPath) -> bool:
    return (path.parts[0] in (TESTS_DIR, CONTRACTS_DIR)
            or COMPONENT_TESTS_DIR in path.parts or BROWSER_TESTS_DIR in path.parts
            or any(part.lower() in EXCLUDED for part in path.parts))


def _service(path: PurePosixPath) -> PurePosixPath | None:
    return None if _outside_sources(path) else path


def _suite(path: PurePosixPath) -> PurePosixPath | None:
    if path.parts[0] != TESTS_DIR or len(path.parts) < 2:
        return None
    return PurePosixPath(*path.parts[1:])


def _documentation(path: PurePosixPath) -> PurePosixPath | None:
    if path.suffix.lower() not in DOCUMENTATION_SUFFIXES or _outside_sources(path):
        return None
    return path


def _component_tests(path: PurePosixPath) -> PurePosixPath | None:
    for marker, member in ((COMPONENT_TESTS_DIR, PARTS_DIR), (BROWSER_TESTS_DIR, BROWSER_MEMBER)):
        if marker not in path.parts:
            continue
        index = path.parts.index(marker)
        root, rest = path.parts[:index], path.parts[index + 1:]
        if not rest:
            return None
        if not root:
            return PurePosixPath(*rest) if marker == COMPONENT_TESTS_DIR else None
        return PurePosixPath(member, *root, *rest)
    return None


def _within(tree: Path, relative: PurePosixPath) -> Path | None:
    destination = tree / relative
    try:
        destination.resolve().relative_to(tree.resolve())
    except ValueError:
        return None
    return destination


def _shown_before(tree: Path, path: PurePosixPath, mapping: Mapping,
                  parent_dirs: dict[str, list[str]]) -> bool:
    """Whether a file the base branch added at repository path `path` belongs in `tree`:
    its nearest directory the branch had is the repository's root, or holds files the
    tree has."""
    for ancestor in path.parents:
        key = ancestor.as_posix()
        if key == '.':
            return True
        if key in parent_dirs:
            return any((mapped := mapping(PurePosixPath(item))) is not None
                       and (tree / mapped).is_file() for item in parent_dirs[key])
    return True


def refresh_sources(workspace: SessionWorkspace, changed: list[str],
                    parent_files: list[str]) -> list[str]:
    """Copy into the workspace's sources the files the merge in its checkout changed
    (`changed`, relative to the commit it was merged into, which had `parent_files`):
    written where the source tree has them or, for a file the base branch added, where
    the tree keeps such files, and deleted where the merge deleted them. The trees are
    the service, the contract tests, the component tests and the documentation, each
    only when the session has it. The paths refreshed."""
    checkout = workspace.checkout
    known = set(parent_files)
    directories: dict[str, list[str]] = {}
    for item in parent_files:
        for ancestor in PurePosixPath(item).parents:
            if ancestor.as_posix() != '.':
                directories.setdefault(ancestor.as_posix(), []).append(item)
    trees: list[tuple[Path, Mapping]] = [
        (workspace.service, _service), (workspace.suite, _suite),
        (workspace.component_tests, _component_tests),
        (workspace.documentation, _documentation)]
    refreshed: list[str] = []
    for name in changed:
        path = PurePosixPath(name)
        if not path.parts or '..' in path.parts or path.is_absolute():
            continue
        source = checkout / path
        present = source.is_file() and not source.is_symlink()
        for tree, mapping in trees:
            relative = mapping(path)
            if relative is None or not tree.is_dir():
                continue
            destination = _within(tree, relative)
            if destination is None:
                continue
            if not present:
                if destination.is_file() or destination.is_symlink():
                    destination.unlink()
                    refreshed.append(name)
                continue
            if not destination.is_file() and (
                    name in known
                    or not _shown_before(tree, path, mapping, directories)):
                # A file the tree left out, or one of a place it keeps nothing of.
                continue
            if destination.is_symlink():
                destination.unlink()
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_file(source, destination)
            refreshed.append(name)
    return refreshed


def _contract_files(directory: Path) -> dict[str, str]:
    if not directory.is_dir() or directory.is_symlink():
        return {}
    return {item.name: item.read_text(encoding='utf-8', errors='replace')
            for item in directory.iterdir() if item.is_file() and not item.is_symlink()}


def _read_record(workspace: SessionWorkspace) -> dict[str, dict[str, str]] | None:
    try:
        record = json.loads((workspace.root / RECORD_DIR / CONTRACT_RECORD).read_text())
    except (OSError, ValueError):
        return None
    if (not isinstance(record, dict) or not isinstance(record.get('own'), dict)
            or not isinstance(record.get('merged'), dict)):
        return None
    return record


def record_merged_contract(workspace: SessionWorkspace, own: dict[str, str]) -> None:
    """Record the contract the merge in the workspace's checkout combined, beside the
    session's own (`own`, the top-level contract files of the commit it was merged
    into). Of a file an earlier merge combined and the session pushed as combined, the
    session's own stays the one recorded then."""
    merged = _contract_files(workspace.checkout / CONTRACTS_DIR)
    earlier = _read_record(workspace)
    if earlier is not None:
        mine: dict[str, str] = {}
        for name in own.keys() | earlier['own'].keys() | earlier['merged'].keys():
            if own.get(name) == earlier['merged'].get(name):
                if name in earlier['own']:
                    mine[name] = earlier['own'][name]
            elif name in own:
                mine[name] = own[name]
        own = mine
    directory = workspace.root / RECORD_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / CONTRACT_RECORD).write_text(json.dumps({'own': own, 'merged': merged}))


def keep_merged_contract(workspace: SessionWorkspace, target: Path) -> None:
    """After the session's own contract was written into `target`'s contracts/, put
    back the files the last base merge combined wherever the written file is still the
    session's own as it was merged: the base branch's requirements, API and architecture
    stay. A file the session changed since stays as written."""
    record = _read_record(workspace)
    if record is None:
        return
    directory = target / CONTRACTS_DIR
    written = _contract_files(directory)
    own, merged = record['own'], record['merged']
    for name in own.keys() | merged.keys():
        if written.get(name) != own.get(name) or written.get(name) == merged.get(name):
            continue
        path = directory / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            continue
        if name in merged:
            directory.mkdir(exist_ok=True)
            path.write_text(merged[name], encoding='utf-8')
        elif path.is_file():
            path.unlink()
