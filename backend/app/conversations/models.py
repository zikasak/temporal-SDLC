from typing import Literal, NotRequired, TypedDict

from app.github.models import RepositorySelection


class AttachmentMetadata(TypedDict):
    document_id: str
    filename: str
    content_type: str
    size_bytes: int


class ConversationMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str
    attachment: NotRequired[AttachmentMetadata]


class LLMRequestRecord(TypedDict):
    request_id: str
    status: str
    result: str | None
    error: str | None
    messages: list[ConversationMessage]
    parent_request_id: str | None
    session_id: str
    generated_documents: list[dict[str, object]]
    repository: RepositorySelection | None


class ForkOrigin(TypedDict):
    """The session and message a forked session was copied from."""

    session_id: str
    message_index: int
