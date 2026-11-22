import logging
from collections.abc import Sequence
from typing import Any

import psycopg
from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from temporalio.service import RPCError, RPCStatusCode

from app.conversations.errors import (
    ConversationNotReady,
    ConversationNotRetryable,
    ConversationTooLarge,
)
from app.documents import DocumentNotFound, DocumentParseError, DocumentTooLarge
from app.temporal.client import TemporalUnavailable
from app.temporal.registry import is_registered
from app.temporal.sessions import InvalidForkPoint, SessionBusy, SessionNotFound
from app.github.errors import GitHubError

logger = logging.getLogger(__name__)

TEMPORAL_UNAVAILABLE = "Temporal is unavailable"
STORAGE_UNAVAILABLE = "Request storage is unavailable"
# Path parameters that name a session or chat request kept in Temporal.
_ID_PARAMETERS = ("session_id", "request_id")


class TemporalUnavailableError(HTTPException):
    """503 for a Temporal failure; refined to 404 for IDs this instance never started."""

    def __init__(self, detail: str = TEMPORAL_UNAVAILABLE) -> None:
        super().__init__(status_code=503, detail=detail)


async def temporal_unavailable_response(request: Request, exc: Exception) -> JSONResponse:
    """Answer a request Temporal could not serve: 404 when the session or request it
    names is unknown to this instance's records, 503 otherwise."""
    for name in _ID_PARAMETERS:
        workflow_id = request.path_params.get(name)
        if not workflow_id:
            continue
        try:
            known = await is_registered(workflow_id)
        except (OSError, psycopg.Error):
            logger.error("Request storage failed", exc_info=True)
            return JSONResponse(status_code=503,
                                content={"detail": "Temporal and request storage are unavailable"})
        if not known:
            detail = "Request not found" if name == "request_id" else "Session not found"
            return JSONResponse(status_code=404, content={"detail": detail})
    detail = exc.detail if isinstance(exc, HTTPException) else TEMPORAL_UNAVAILABLE
    return JSONResponse(status_code=503, content={"detail": detail or TEMPORAL_UNAVAILABLE})


def validation_detail(errors: Sequence[Any]) -> str:
    """One readable message for validation errors, built from their locations and
    messages only: the submitted inputs could hold secrets."""
    parts = []
    for error in errors:
        loc = [str(part) for part in error.get('loc', ()) if part != 'body']
        msg = str(error.get('msg') or 'Invalid value')
        parts.append(f"{'.'.join(loc)}: {msg}" if loc else msg)
    return '; '.join(parts) or 'The request is invalid.'


async def request_validation_response(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={'detail': validation_detail(exc.errors())})


def http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, TemporalUnavailable):
        return TemporalUnavailableError()
    if isinstance(exc, SessionNotFound):
        return _client_error(404, exc)
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
        return TemporalUnavailableError()
    if isinstance(exc, (OSError, psycopg.Error)):
        logger.error("Request storage failed", exc_info=exc)
        return HTTPException(status_code=503, detail=STORAGE_UNAVAILABLE)
    raise exc


def _client_error(status_code: int, exc: Exception, detail: str | None = None) -> HTTPException:
    logger.warning("%s: %s", type(exc).__name__, exc)
    return HTTPException(status_code=status_code, detail=detail or str(exc))
