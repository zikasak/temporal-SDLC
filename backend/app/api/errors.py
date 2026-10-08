import logging

import psycopg
from fastapi import HTTPException
from temporalio.service import RPCError, RPCStatusCode

from app.conversations.errors import (
    ConversationNotReady,
    ConversationNotRetryable,
    ConversationTooLarge,
)
from app.documents import DocumentNotFound, DocumentParseError, DocumentTooLarge
from app.temporal.sessions import InvalidForkPoint, SessionBusy
from app.github.errors import GitHubError

logger = logging.getLogger(__name__)


def http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, GitHubError):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        return HTTPException(status_code=exc.status_code, detail=str(exc), headers=headers)
    if isinstance(exc, (ConversationNotReady, ConversationNotRetryable, DocumentNotFound,
                        SessionBusy)):
        return _client_error(409, exc)
    if isinstance(exc, (ConversationTooLarge, DocumentTooLarge)):
        return _client_error(413, exc)
    if isinstance(exc, DocumentParseError):
        return _client_error(415, exc)
    if isinstance(exc, InvalidForkPoint):
        return _client_error(422, exc)
    if isinstance(exc, RPCError):
        if exc.status == RPCStatusCode.NOT_FOUND:
            return _client_error(404, exc, "Request not found")
        logger.error("Temporal request failed", exc_info=exc)
        return HTTPException(status_code=503, detail="Temporal is unavailable")
    if isinstance(exc, (OSError, psycopg.Error)):
        logger.error("Request storage failed", exc_info=exc)
        return HTTPException(status_code=503, detail="Request storage is unavailable")
    raise exc


def _client_error(status_code: int, exc: Exception, detail: str | None = None) -> HTTPException:
    logger.warning("%s: %s", type(exc).__name__, exc)
    return HTTPException(status_code=status_code, detail=detail or str(exc))
