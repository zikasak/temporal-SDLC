"""Native agent sessions (Claude's, Pi's) saved between the turns of a conversation
(app.llm.saved_conversations): the files of the session a later turn resumes."""
import io
import json
import logging
import re
import shutil
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage

from app.asyncio_utils import run_sync
from app.llm.saved_conversations import ConversationRef, continues, saved_meta

logger = logging.getLogger(__name__)
META_FILE = 'conversation.json'


@dataclass(frozen=True)
class SessionArchive:
    """What of an agent's session directory a later turn needs."""

    # The provider's name in the saved object's name.
    provider: str
    # Entries of the session directory that are saved.
    entries: tuple[str, ...]
    # The session's transcript below the directory, by its ID.
    transcript: str

    def object_name(self, conversation: str) -> str:
        return f'{conversation}/{self.provider}.tar.gz'

    def pack(self, directory: Path, meta: dict[str, Any]) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            encoded = json.dumps(meta).encode()
            info = tarfile.TarInfo(META_FILE)
            info.size = len(encoded)
            archive.addfile(info, io.BytesIO(encoded))
            for name in self.entries:
                if (directory / name).exists():
                    archive.add(directory / name, arcname=name)
        return buffer.getvalue()

    def unpack(self, data: bytes, directory: Path) -> dict[str, Any]:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            meta_file = archive.extractfile(META_FILE)
            if meta_file is None:
                raise ValueError('The saved session has no metadata')
            meta = json.loads(meta_file.read())
            archive.extractall(directory, members=[
                member for member in archive.getmembers()
                if member.name.split('/', 1)[0] in self.entries], filter='data')
        return meta

    def clear(self, directory: Path) -> None:
        for name in self.entries:
            shutil.rmtree(directory / name, ignore_errors=True)

    def transcripts(self, directory: Path, session: str) -> list[Path]:
        if not re.fullmatch(r'[A-Za-z0-9-]+', session):
            return []
        return sorted(directory.glob(self.transcript.format(session=session)))

    def resumable(self, meta: dict[str, Any] | None, history: Sequence[BaseMessage],
                  directory: Path, context: str) -> int:
        """How many of `history`'s messages the restored session holds, or 0 when it
        cannot continue this conversation."""
        session = (meta or {}).get('session_id')
        if not isinstance(session, str) or not self.transcripts(directory, session):
            return 0
        return continues(meta, history, context)

    async def restore(self, store, conversation: ConversationRef,
                      history: list[BaseMessage], directory: Path) -> tuple[str | None, int]:
        """Restore the conversation's saved session into `directory`: its ID and how many
        of `history`'s messages it holds, or (None, 0) when this turn starts anew."""
        try:
            data = await run_sync(store.load, self.object_name(conversation.source))
            if data is None:
                return None, 0
            meta = await run_sync(self.unpack, data, directory)
        except Exception:
            logger.warning('Could not restore the %s session of %s; starting a new one',
                           self.provider, conversation.source, exc_info=True)
            return None, 0
        resumed = await run_sync(self.resumable, meta, history, directory,
                                 conversation.context)
        if not resumed:
            logger.info('The saved %s session of %s does not continue this conversation; '
                        'starting a new one', self.provider, conversation.source)
            # Not saved again with the new session.
            await run_sync(self.clear, directory)
            return None, 0
        logger.info('Resuming %s session %s of %s after %d messages', self.provider,
                    meta['session_id'], conversation.source, resumed)
        return meta['session_id'], resumed

    async def save(self, store, conversation: ConversationRef, history: list[BaseMessage],
                   session: str, directory: Path) -> None:
        # The conversation goes on from this session's first messages and its final
        # reply; the corrections a turn sent in between are not part of it.
        meta = {'session_id': session, **saved_meta(history, conversation.context)}
        try:
            data = await run_sync(self.pack, directory, meta)
            await run_sync(store.save, self.object_name(conversation.name), data)
        except Exception:
            logger.warning('Could not save the %s session of %s', self.provider,
                           conversation.name, exc_info=True)
            return
        logger.info('Saved %s session %s of %s (%d bytes)', self.provider, session,
                    conversation.name, len(data))


# The transcripts and the tracing plugin's progress, so a resumed session does not
# export earlier turns again.
CLAUDE = SessionArchive('claude', ('projects', '.langfuse-state'),
                        'projects/*/{session}.jsonl')
# Pi names a session's file by its start time and ID.
PI = SessionArchive('pi', ('sessions',), 'sessions/*_{session}.jsonl')
# Pi calling the OpenAI endpoint saved in Settings (the openai adapter).
PI_ENDPOINT = SessionArchive('openai', PI.entries, PI.transcript)
