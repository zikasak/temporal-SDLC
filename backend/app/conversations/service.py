"""LLM requests: each message is a Temporal workflow whose memo holds the conversation."""

import json
import logging
import uuid
from datetime import datetime, timedelta

from temporalio.client import Client, WorkflowExecutionStatus

from app.config import TASK_QUEUE
from app.conversations.errors import (
    ConversationNotReady,
    ConversationNotRetryable,
    ConversationTooLarge,
)
from app.conversations.models import AttachmentMetadata, ConversationMessage, LLMRequestRecord
from app.documents import (
    DocumentNotFound,
    copy_generated_documents,
    copy_session_passages,
    get_document,
    list_generated_documents,
    touch_documents,
    touch_generated_documents,
)
from app.github.models import RepositorySelection
from app.github.errors import GitHubError
from app.temporal.client import connect_temporal
from app.temporal.sessions import InvalidForkPoint, fork_messages

logger = logging.getLogger(__name__)

MAX_CONVERSATION_BYTES = 64 * 1024


async def start_llm_request(
    message: str,
    parent_request_id: str | None = None,
    attachment: AttachmentMetadata | None = None,
    fork_after: int | None = None,
    repository: RepositorySelection | None = None,
) -> str:
    """Start a conversation, reply to a completed request, or, with `fork_after`,
    reply to an earlier answer in the parent's conversation as a new branch."""
    request_id = _new_request_id()
    if not parent_request_id:
        return await _start(request_id, [], message, attachment, None, request_id, repository)
    if repository is not None:
        raise GitHubError(422, "Repository selection is inherited and cannot be changed.")
    parent = await get_llm_request(parent_request_id)
    if fork_after is None:
        if parent["status"] != "completed" or not parent["messages"]:
            raise ConversationNotReady(
                "Only completed requests with conversation history can be continued."
            )
        return await _start(request_id, parent["messages"], message, attachment,
                            parent_request_id, parent["session_id"], parent.get("repository"))
    prior = fork_messages(parent["messages"], fork_after)
    if prior[-1]["role"] != "assistant":
        raise InvalidForkPoint(
            "Only an answer can be followed by a new message; fork after your own "
            "message to have it answered again."
        )
    return await _start(request_id, prior, message, attachment, parent_request_id,
                        await _fork_session(parent, fork_after, request_id), parent.get("repository"))


async def fork_llm_request(request_id: str, message_index: int) -> str:
    """Fork the conversation after one of your messages and answer it again.

    Forking after an answer needs a new message: see `start_llm_request`."""
    source = await get_llm_request(request_id)
    kept = fork_messages(source["messages"], message_index)
    pending = kept.pop()
    if pending["role"] != "user":
        raise InvalidForkPoint("Only your own messages can be answered again.")
    new_request_id = _new_request_id()
    session_id = await _fork_session(source, message_index - 1, new_request_id)
    return await _start(new_request_id, kept, pending["content"], pending.get("attachment"),
                        request_id, session_id, source.get("repository"))


async def retry_llm_request(request_id: str) -> str:
    previous = await get_llm_request(request_id)
    if previous["status"] not in {"failed", "canceled", "terminated", "timed_out"}:
        raise ConversationNotRetryable("Only an unsuccessful request can be retried.")
    if not previous["messages"] or previous["messages"][-1]["role"] != "user":
        raise ConversationNotRetryable("This request has no message to retry.")
    pending = previous["messages"][-1]
    logger.info("Retrying LLM request %s", request_id)
    new_request_id = _new_request_id()
    return await _start(new_request_id, previous["messages"][:-1], pending["content"],
                        pending.get("attachment"), previous["parent_request_id"],
                        previous["session_id"], previous.get("repository"))


def _new_request_id() -> str:
    return f"llm-request-{uuid.uuid4().hex}"


async def _fork_session(source: LLMRequestRecord, message_index: int, session_id: str) -> str:
    """A new session for a branch, holding the generated documents that existed
    once `messages[message_index]` was answered."""
    if message_index >= 0:
        answered_at = await _answered_at(source["request_id"], message_index)
        if answered_at:
            await copy_generated_documents(source["session_id"], session_id, answered_at)
            await copy_session_passages(source["session_id"], session_id, answered_at)
    logger.info("Forked session %s after message %d as %s",
                source["session_id"], message_index, session_id)
    return session_id


async def _answered_at(request_id: str, message_index: int) -> datetime | None:
    """When the request whose answer is `messages[message_index]` finished.

    Every request's memo holds the conversation before its answer, and its
    ancestors hold shorter prefixes of it."""
    client = await connect_temporal()
    current_id: str | None = request_id
    while current_id:
        description = await client.get_workflow_handle(current_id).describe()
        messages = await description.memo_value("messages", [])
        if len(messages) == message_index:
            return description.close_time
        if len(messages) < message_index:
            return None
        current_id = await description.memo_value("parent_request_id", None) or None
    return None


async def _start(
    request_id: str,
    prior: list[ConversationMessage],
    message: str,
    attachment: AttachmentMetadata | None,
    parent_request_id: str | None,
    session_id: str,
    repository: RepositorySelection | None = None,
) -> str:
    user_message: ConversationMessage = {"role": "user", "content": message}
    if attachment:
        user_message["attachment"] = attachment
    messages = [*prior, user_message]
    encoded = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_CONVERSATION_BYTES:
        raise ConversationTooLarge("This conversation is too long to continue.")

    document_ids = {
        item["attachment"]["document_id"]
        for item in messages
        if item.get("attachment")
    }
    await touch_documents(document_ids)
    await touch_generated_documents(session_id)
    parse_document_id: str | None = None
    if attachment:
        document = await get_document(attachment["document_id"])
        if document["parse_status"] != "parsed":
            parse_document_id = attachment["document_id"]

    client = await connect_temporal()
    handle = await client.start_workflow(
        "LLMRequestWorkflow",
        args=[messages, parse_document_id, session_id, repository],
        id=request_id,
        task_queue=TASK_QUEUE,
        # A session's first message may also have to clone its repository.
        run_timeout=timedelta(minutes=18 if repository else 8),
        memo={"messages": messages, "parent_request_id": parent_request_id or "",
              "session_id": session_id, "repository": repository},
    )
    logger.info("Started LLM request %s (session %s, parent %s, attachment %s)",
                request_id, session_id, parent_request_id,
                attachment["document_id"] if attachment else None)
    return handle.id


async def _session_id_for_request(
    client: Client, request_id: str, description: object
) -> str:
    current_id = request_id
    current_description = description
    while True:
        saved = await current_description.memo_value("session_id", None)
        if saved:
            return saved
        parent_id = await current_description.memo_value("parent_request_id", None)
        if not parent_id:
            return current_id
        current_id = parent_id
        current_description = await client.get_workflow_handle(current_id).describe()


async def get_llm_request(request_id: str) -> LLMRequestRecord:
    client = await connect_temporal()
    handle = client.get_workflow_handle(request_id)
    description = await handle.describe()
    status = description.status
    messages: list[ConversationMessage] = await description.memo_value("messages", [])
    parent_request_id: str | None = await description.memo_value(
        "parent_request_id", None
    ) or None
    session_id = await _session_id_for_request(client, request_id, description)
    generated_documents = await list_generated_documents(session_id)
    repository = await description.memo_value("repository", None)

    if status == WorkflowExecutionStatus.COMPLETED:
        result: str = await handle.result()
        return {
            "request_id": request_id,
            "status": "completed",
            "result": result,
            "error": None,
            "messages": [*messages, {"role": "assistant", "content": result}]
            if messages
            else [],
            "parent_request_id": parent_request_id,
            "session_id": session_id,
            "generated_documents": generated_documents,
            "repository": repository,
        }

    errors = {
        WorkflowExecutionStatus.FAILED: "The LLM request failed.",
        WorkflowExecutionStatus.CANCELED: "The LLM request was canceled.",
        WorkflowExecutionStatus.TERMINATED: "The LLM request was terminated.",
        WorkflowExecutionStatus.TIMED_OUT: "The LLM request timed out.",
    }
    error = errors.get(status) if status else None
    if status == WorkflowExecutionStatus.FAILED and messages:
        attachment = messages[-1].get("attachment")
        if attachment:
            try:
                document = await get_document(attachment["document_id"])
                if document["parse_status"] == "failed":
                    error = document["parse_error"] or "The document could not be parsed."
            except DocumentNotFound:
                pass
    return {
        "request_id": request_id,
        "status": status.name.lower() if status and status in errors else "running",
        "result": None,
        "error": error,
        "messages": messages,
        "parent_request_id": parent_request_id,
        "session_id": session_id,
        "generated_documents": generated_documents,
        "repository": repository,
    }
