"""Files an earlier review round already judged clean, which a later round of the same
run need not review again (plans/deterministic-checks.md, item 1).

After every completed review run the host records, per reviewed file, a hash of its diff
and the number of findings reported on it, with the run's judgement key: what the
reviewer judged by besides the diff. A later round skips a file only when its diff and the
judgement key are both unchanged, the run that last covered it reported nothing on it and
nothing without a path. A file with a finding is always reviewed again: the reviewer, not
the host, decides whether a correction elsewhere settled it."""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

RECORD = 'reviewed.json'
# The host's own note of the run in progress, so that a paused run that is resumed still
# records what it reviewed. Never in the agent's workspace.
PENDING = 'pending-part-{part}.json'
# The runs a record keeps, newest last; older runs no longer decide anything.
KEPT_RUNS = 30
MAX_RECORD_BYTES = 4 * 1024 * 1024
_AGENT = Path(__file__).with_name('agent.py')


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()


def judgement_key(scope: str | None, instructions: str, contracts: dict[str, Any]) -> str:
    """What the reviewer judges a file by besides its diff: the scope, the user's
    instructions, the contracts with the brief, and the reviewer's prompts, whose source
    changes with every change of them."""
    prompts = _AGENT.read_bytes() if _AGENT.is_file() else b''
    return digest(json.dumps({'scope': scope or 'review', 'instructions': instructions,
                              'contracts': contracts,
                              'prompts': hashlib.sha256(prompts).hexdigest()},
                             sort_keys=True, ensure_ascii=False, default=str))


def read_record(artifact_dir: Path) -> list[dict[str, Any]]:
    """The recorded runs, oldest first; none when the record is missing or unreadable."""
    file = artifact_dir / RECORD
    try:
        if file.is_symlink() or file.stat().st_size > MAX_RECORD_BYTES:
            return []
        runs = json.loads(file.read_text(encoding='utf-8')).get('runs')
    except (OSError, ValueError, AttributeError):
        return []
    return [run for run in runs if isinstance(run, dict)] if isinstance(runs, list) else []


def settled_files(runs: list[dict[str, Any]], diffs: dict[str, str], key: str,
                  round_id: str) -> set[str]:
    """The changed files the latest earlier run that covered each judged clean, with the
    same diff and judgement key. Runs of the round in progress (`round_id`) never count,
    so every part of a round sees the same settled files."""
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        if run.get('round') == round_id or not isinstance(run.get('files'), dict):
            continue
        for path in run['files']:
            latest[path] = run
    settled = set()
    for path, diff in diffs.items():
        run = latest.get(path)
        if run is None or run.get('key') != key or run.get('pathless'):
            continue
        entry = run['files'][path]
        if (isinstance(entry, dict) and entry.get('hash') == digest(diff)
                and entry.get('findings') == 0):
            settled.add(path)
    return settled


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                     delete=False, suffix='.tmp') as file:
        json.dump(value, file, ensure_ascii=False)
    os.replace(file.name, path)


def write_pending(artifact_dir: Path, part: int, round_id: str, key: str,
                  diffs: dict[str, str]) -> None:
    """Note the files a run is about to review, with their diff hashes."""
    _write_json(artifact_dir / PENDING.format(part=part + 1), {
        'round': round_id, 'key': key,
        'files': {path: digest(diff) for path, diff in diffs.items()}})


def record_run(artifact_dir: Path, part: int, findings: list[dict[str, Any]]) -> None:
    """Record the run noted by `write_pending` as completed with these findings."""
    pending = artifact_dir / PENDING.format(part=part + 1)
    try:
        noted = json.loads(pending.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    counts = {path: 0 for path in noted.get('files') or {}}
    pathless = False
    for finding in findings:
        path = finding.get('path')
        if not path:
            pathless = True
        elif path in counts:
            counts[path] += 1
    run = {'round': noted.get('round'), 'key': noted.get('key'), 'pathless': pathless,
           'files': {path: {'hash': hashed, 'findings': counts[path]}
                     for path, hashed in (noted.get('files') or {}).items()}}
    runs = [*read_record(artifact_dir), run][-KEPT_RUNS:]
    _write_json(artifact_dir / RECORD, {'runs': runs})
    pending.unlink(missing_ok=True)
