"""What a base merge took into a pull request branch, kept by the session's later pushes.

A base merge fix round merges the base branch into the pull request branch in the
session's working copy alone. The sources later pushes are built from (the service, the
contract tests, the component tests and their stubs, the documentation) are brought up
to the merge here, and the merged contract is recorded, so that a later push, which
writes the contract from the delivery's own, keeps what the merge combined.

    <session>/base-merge/ours/    contracts/ files as the delivery's own push had them
    <session>/base-merge/merged/  the same files as the merge combined them
"""
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.agent_workflows.host import copy_file
from app.github.checkout import CONTRACTS_DIR

BASE_MERGE_DIR = 'base-merge'
OURS = 'ours'
MERGED = 'merged'


@dataclass(frozen=True)
class SourceMapping:
    """Where the repository's files under `prefix` are kept among a session's sources:
    `source`, for those `include` accepts by their path relative to `prefix`."""
    prefix: str
    source: Path
    include: Callable[[Path], bool]


def refresh_sources(checkout: Path, changed: list[str],
                    mappings: list[SourceMapping]) -> list[str]:
    """Bring the session's sources up to the merged `checkout`: every path of `changed`
    a mapping covers is copied from the checkout into its source, or deleted from it
    when the merge removed it. The source paths updated."""
    updated: list[str] = []
    for path in changed:
        relative_path = PurePosixPath(path)
        if '..' in relative_path.parts or relative_path.is_absolute():
            continue
        merged = checkout / path
        for mapping in mappings:
            if mapping.prefix:
                try:
                    relative = Path(relative_path.relative_to(mapping.prefix))
                except ValueError:
                    continue
            else:
                relative = Path(relative_path)
            if not relative.parts or relative.parts[0] == '.git' or not mapping.include(relative):
                continue
            root = mapping.source.resolve()
            destination = mapping.source / relative
            if not destination.parent.resolve().is_relative_to(root):
                continue
            if merged.is_file() and not merged.is_symlink():
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.is_symlink() or destination.is_dir():
                    _remove(destination)
                copy_file(merged, destination)
            elif destination.is_file() or destination.is_symlink():
                destination.unlink()
            else:
                continue
            updated.append(str(destination))
    return updated


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _read(directory: Path) -> dict[str, bytes]:
    if not directory.is_dir():
        return {}
    return {item.name: item.read_bytes() for item in directory.iterdir()
            if item.is_file() and not item.is_symlink()}


def _write(directory: Path, files: dict[str, bytes]) -> None:
    if directory.exists():
        _remove(directory)
    directory.mkdir(parents=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)


def record_merged_contract(session_dir: Path, ours: dict[str, bytes],
                           merged: dict[str, bytes]) -> None:
    """Record the contract files a base merge combined (`merged`) beside those of the
    pushed tip it was merged into (`ours`). A file that tip took from an earlier base
    merge is recorded by what the delivery's own push had in it before that merge, so
    that later pushes, which write the delivery's own contract, keep every merge."""
    directory = session_dir / BASE_MERGE_DIR
    earlier_ours, earlier_merged = _read(directory / OURS), _read(directory / MERGED)
    own: dict[str, bytes] = {}
    for name in ours.keys() | earlier_ours.keys() | earlier_merged.keys():
        if name in earlier_merged.keys() | earlier_ours.keys() \
                and ours.get(name) == earlier_merged.get(name):
            if name in earlier_ours:
                own[name] = earlier_ours[name]
        elif name in ours:
            own[name] = ours[name]
    _write(directory / OURS, own)
    _write(directory / MERGED, merged)


def keep_merged_contract(session_dir: Path, checkout: Path) -> None:
    """After a push's sources were written into `checkout`: every contract file (in
    contracts/, without its component and part contracts) still as the delivery's own
    push had it before a base merge is made as that merge combined it, so that the
    base branch's requirements, API and architecture stay. A file the delivery changed
    since stays as it is now. Nothing changes for a session without a base merge."""
    directory = session_dir / BASE_MERGE_DIR
    if not (directory / MERGED).is_dir():
        return
    ours, merged = _read(directory / OURS), _read(directory / MERGED)
    contracts = checkout / CONTRACTS_DIR
    for name in ours.keys() | merged.keys():
        path = contracts / name
        if path.is_symlink() or path.is_dir():
            continue
        written = path.read_bytes() if path.is_file() else None
        if written != ours.get(name) or written == merged.get(name):
            continue
        if name in merged:
            contracts.mkdir(exist_ok=True)
            path.write_bytes(merged[name])
        else:
            path.unlink()
