"""Provider conversations saved between the turns of an application conversation.

Every turn of a conversation runs in a new activity, possibly on another worker, and the
application keeps only its users' messages and the replies. What the provider saw beside
them (tool calls and results, Claude's native session) is saved to S3-compatible storage
after a turn and restored before the next, which then sends only what is new.

A saved conversation records how many of the application's messages it holds: those up
to the last user message of its turn, sent then with that turn's current state, and its
reply. It continues a later turn only when the messages before them are unchanged and
the conversation has gone on since."""
import functools
import hashlib
import json
import logging
from collections.abc import Sequence
from typing import Any, Protocol
from urllib.parse import quote

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

logger = logging.getLogger(__name__)
OBJECT_PREFIX = 'conversations/'


class ConversationStore(Protocol):
    def load(self, name: str) -> bytes | None: ...

    def save(self, name: str, data: bytes) -> None: ...


class S3ConversationStore:
    def __init__(self, client: Any, bucket: str):
        self.client, self.bucket = client, bucket

    @staticmethod
    def object_key(name: str) -> str:
        return OBJECT_PREFIX + quote(name, safe='/-_.')

    def load(self, name: str) -> bytes | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.object_key(name))
        except self.client.exceptions.NoSuchKey:
            return None
        with response['Body'] as body:
            return body.read()

    def save(self, name: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.object_key(name), Body=data)


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
def get_store() -> ConversationStore | None:
    """The storage configured by the S3_* environment variables, or None without it."""
    from app import config

    if not (config.S3_BUCKET and config.S3_ACCESS_KEY_ID and config.S3_SECRET_ACCESS_KEY):
        logger.warning('S3 storage is not configured; every conversation turn starts the '
                       'provider conversation anew')
        return None
    return S3ConversationStore(
        s3_client(config.S3_ENDPOINT_URL, config.S3_REGION, config.S3_ACCESS_KEY_ID,
                  config.S3_SECRET_ACCESS_KEY), config.S3_BUCKET)


def fingerprint(history: Sequence[BaseMessage]) -> str:
    return hashlib.sha256(json.dumps(
        [[message.type, message.text] for message in history], ensure_ascii=False,
    ).encode()).hexdigest()


def saved_meta(history: Sequence[BaseMessage]) -> dict[str, Any]:
    """What a turn that started from `history` and replied records."""
    return {'consumed': len(history) + 1, 'fingerprint': fingerprint(history[:-1])}


def continues(meta: dict[str, Any] | None, history: Sequence[BaseMessage]) -> int:
    """How many of `history`'s messages the saved conversation holds, or 0 when it
    cannot continue this conversation."""
    if not meta:
        return 0
    consumed = meta.get('consumed')
    if not isinstance(consumed, int) or not 2 <= consumed < len(history):
        return 0
    if not (isinstance(history[consumed - 2], HumanMessage)
            and isinstance(history[consumed - 1], AIMessage)):
        return 0
    if fingerprint(history[:consumed - 2]) != meta.get('fingerprint'):
        return 0
    return consumed
