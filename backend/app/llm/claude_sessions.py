"""Native Claude sessions saved between the turns of a conversation
(app.llm.saved_conversations): the session's transcript and the tracing plugin's export
progress, which the next turn resumes."""
import io
import json
import re
import shutil
import tarfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage

from app.llm.saved_conversations import continues

META_FILE = 'conversation.json'
# What of a Claude configuration directory a later turn needs: the transcripts and the
# tracing plugin's progress, so a resumed session does not export earlier turns again.
SAVED_ENTRIES = ('projects', '.langfuse-state')


def object_name(conversation: str) -> str:
    return f'{conversation}/claude.tar.gz'


def pack(config_dir: Path, meta: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        encoded = json.dumps(meta).encode()
        info = tarfile.TarInfo(META_FILE)
        info.size = len(encoded)
        archive.addfile(info, io.BytesIO(encoded))
        for name in SAVED_ENTRIES:
            if (config_dir / name).exists():
                archive.add(config_dir / name, arcname=name)
    return buffer.getvalue()


def unpack(data: bytes, config_dir: Path) -> dict[str, Any]:
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        meta_file = archive.extractfile(META_FILE)
        if meta_file is None:
            raise ValueError('The saved session has no metadata')
        meta = json.loads(meta_file.read())
        archive.extractall(config_dir, members=[
            member for member in archive.getmembers()
            if member.name.split('/', 1)[0] in SAVED_ENTRIES], filter='data')
    return meta


def clear(config_dir: Path) -> None:
    for name in SAVED_ENTRIES:
        shutil.rmtree(config_dir / name, ignore_errors=True)


def resumable(meta: dict[str, Any] | None, history: Sequence[BaseMessage],
              config_dir: Path, context: str) -> int:
    """How many of `history`'s messages the restored session holds, or 0 when it cannot
    continue this conversation."""
    session = (meta or {}).get('session_id')
    if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9-]+', session):
        return 0
    if not any(config_dir.glob(f'projects/*/{session}.jsonl')):
        return 0
    return continues(meta, history, context)
