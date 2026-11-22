"""Native Claude sessions saved between the turns of an application conversation.

Every turn of a conversation runs in a new activity, possibly on another worker. The
session's transcript (and the tracing plugin's export progress) is saved to S3-compatible
storage after a turn and restored before the next, which then resumes the same native
session and sends only the new user message."""
import functools
import hashlib
import io
import json
import logging
import re
import shutil
import tarfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

logger = logging.getLogger(__name__)
OBJECT_PREFIX = 'claude-sessions/'
META_FILE = 'conversation.json'
# What of a Claude configuration directory a later turn needs: the transcripts and the
# tracing plugin's progress, so a resumed session does not export earlier turns again.
SAVED_ENTRIES = ('projects', '.langfuse-state')


class ConversationStore(Protocol):
    def load(self, key: str) -> bytes | None: ...

    def save(self, key: str, data: bytes) -> None: ...


class S3ConversationStore:
    def __init__(self, client: Any, bucket: str):
        self.client, self.bucket = client, bucket

    @staticmethod
    def object_key(key: str) -> str:
        return OBJECT_PREFIX + quote(key, safe='/-_.') + '.tar.gz'

    def load(self, key: str) -> bytes | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.object_key(key))
        except self.client.exceptions.NoSuchKey:
            return None
        with response['Body'] as body:
            return body.read()

    def save(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.object_key(key), Body=data,
                               ContentType='application/gzip')


def s3_client(endpoint_url: str | None, region: str, access_key: str, secret_key: str) -> Any:
    import boto3
    from botocore.config import Config

    # The storage serves buckets only under the endpoint's path, never as subdomains.
    return boto3.client(
        's3', endpoint_url=endpoint_url, region_name=region,
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        config=Config(s3={'addressing_style': 'path'}, signature_version='s3v4',
                      retries={'max_attempts': 5, 'mode': 'standard'},
                      connect_timeout=10, read_timeout=60))


@functools.cache
def conversation_store() -> ConversationStore | None:
    """The storage configured by the S3_* environment variables, or None without it."""
    from app import config

    if not (config.S3_BUCKET and config.S3_ACCESS_KEY_ID and config.S3_SECRET_ACCESS_KEY):
        logger.warning('S3 storage is not configured; Claude conversations start a new '
                       'native session on every turn')
        return None
    return S3ConversationStore(
        s3_client(config.S3_ENDPOINT_URL, config.S3_REGION, config.S3_ACCESS_KEY_ID,
                  config.S3_SECRET_ACCESS_KEY), config.S3_BUCKET)


def fingerprint(history: Sequence[BaseMessage]) -> str:
    return hashlib.sha256(json.dumps(
        [[message.type, message.text] for message in history], ensure_ascii=False,
    ).encode()).hexdigest()


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
              config_dir: Path) -> int:
    """How many of `history`'s messages the saved session already holds, or 0 when it
    cannot continue this conversation. The session holds the messages up to its last
    user message, sent then with that turn's current state, and its reply; the messages
    before them must be unchanged, and the conversation must have gone on since."""
    if not meta:
        return 0
    consumed, session = meta.get('consumed'), meta.get('session_id')
    if not isinstance(consumed, int) or not isinstance(session, str) \
            or not re.fullmatch(r'[A-Za-z0-9-]+', session):
        return 0
    if not 2 <= consumed < len(history):
        return 0
    if not (isinstance(history[consumed - 2], HumanMessage)
            and isinstance(history[consumed - 1], AIMessage)):
        return 0
    if fingerprint(history[:consumed - 2]) != meta.get('fingerprint'):
        return 0
    if not any(config_dir.glob(f'projects/*/{session}.jsonl')):
        return 0
    return consumed
