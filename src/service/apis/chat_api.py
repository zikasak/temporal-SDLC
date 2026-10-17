# coding: utf-8

from typing import Dict, List  # noqa: F401
import importlib
import pkgutil

from service.apis.chat_api_base import BaseChatApi
import service.impl

from fastapi import (  # noqa: F401
    APIRouter,
    Body,
    Cookie,
    Depends,
    Form,
    Header,
    HTTPException,
    Path,
    Query,
    Response,
    Security,
    status,
)

from service.models.extra_models import TokenModel  # noqa: F401
from pydantic import Field, StrictStr
from typing import Any
from typing_extensions import Annotated
from service.models.error import Error
from service.models.fork_input import ForkInput
from service.models.llm_request import LLMRequest
from service.models.llm_request_input import LLMRequestInput
from service.models.llm_request_started import LLMRequestStarted
from service.security_api import get_token_bearer

router = APIRouter()

ns_pkg = service.impl
for _, name, _ in pkgutil.iter_modules(ns_pkg.__path__, ns_pkg.__name__ + "."):
    importlib.import_module(name)


@router.post(
    "/llm/requests",
    responses={
        202: {"model": LLMRequestStarted, "description": "Accepted"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        400: {"model": Error, "description": "Bad Request"},
        409: {"model": Error, "description": "Conflict"},
        413: {"model": Error, "description": "Request Entity Too Large"},
        415: {"model": Error, "description": "Unsupported Media Type"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["chat"],
    summary="Send a chat request",
    response_model_by_alias=True,
)
async def post_llm_requests(
    llm_request_input: LLMRequestInput = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> LLMRequestStarted:
    """JSON, or multipart form data with at most one file."""
    if not BaseChatApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseChatApi.subclasses[0]().post_llm_requests(llm_request_input)


@router.get(
    "/llm/requests/{request_id}",
    responses={
        200: {"model": LLMRequest, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
    },
    tags=["chat"],
    summary="Get a chat request",
    response_model_by_alias=True,
)
async def get_llm_requests_by_request_id(
    request_id: Annotated[StrictStr, Field(description="llm-request- followed by 32 hex digits.")] = Path(..., description="llm-request- followed by 32 hex digits."),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> LLMRequest:
    """The request&#39;s status and conversation."""
    if not BaseChatApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseChatApi.subclasses[0]().get_llm_requests_by_request_id(request_id)


@router.get(
    "/llm/requests/{request_id}/documents/{document_id}",
    responses={
        200: {"description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
    },
    tags=["chat"],
    summary="Download a document",
    response_model_by_alias=True,
)
async def get_llm_requests_by_request_id_documents_by_document_id(
    request_id: Annotated[StrictStr, Field(description="llm-request- followed by 32 hex digits.")] = Path(..., description="llm-request- followed by 32 hex digits."),
    document_id: Annotated[StrictStr, Field(description="32 hex digits.")] = Path(..., description="32 hex digits."),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> None:
    """An attachment of the conversation or a document it generated, as a file."""
    if not BaseChatApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseChatApi.subclasses[0]().get_llm_requests_by_request_id_documents_by_document_id(request_id, document_id)


@router.post(
    "/llm/requests/{request_id}/fork",
    responses={
        202: {"model": LLMRequestStarted, "description": "Accepted"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["chat"],
    summary="Fork a chat conversation",
    response_model_by_alias=True,
)
async def post_llm_requests_by_request_id_fork(
    request_id: Annotated[StrictStr, Field(description="llm-request- followed by 32 hex digits.")] = Path(..., description="llm-request- followed by 32 hex digits."),
    fork_input: ForkInput = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> LLMRequestStarted:
    """Branches after one of the user&#39;s messages and answers it again."""
    if not BaseChatApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseChatApi.subclasses[0]().post_llm_requests_by_request_id_fork(request_id, fork_input)


@router.post(
    "/llm/requests/{request_id}/retry",
    responses={
        202: {"model": LLMRequestStarted, "description": "Accepted"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
    },
    tags=["chat"],
    summary="Retry a chat request",
    response_model_by_alias=True,
)
async def post_llm_requests_by_request_id_retry(
    request_id: Annotated[StrictStr, Field(description="llm-request- followed by 32 hex digits.")] = Path(..., description="llm-request- followed by 32 hex digits."),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> LLMRequestStarted:
    """Answers the failed request again."""
    if not BaseChatApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseChatApi.subclasses[0]().post_llm_requests_by_request_id_retry(request_id)
