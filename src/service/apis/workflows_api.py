# coding: utf-8

from typing import Dict, List  # noqa: F401
import importlib
import pkgutil

from service.apis.workflows_api_base import BaseWorkflowsApi
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
from typing import Optional
from typing_extensions import Annotated
from service.models.error import Error
from service.models.workflow_page import WorkflowPage
from service.models.workflow_result import WorkflowResult
from service.security_api import get_token_bearer

router = APIRouter()

ns_pkg = service.impl
for _, name, _ in pkgutil.iter_modules(ns_pkg.__path__, ns_pkg.__name__ + "."):
    importlib.import_module(name)


@router.post(
    "/start",
    responses={
        200: {"model": WorkflowResult, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
    },
    tags=["workflows"],
    summary="Start the sample workflow",
    response_model_by_alias=True,
)
async def post_start(
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> WorkflowResult:
    """Runs the sample greeting workflow and returns its result."""
    if not BaseWorkflowsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseWorkflowsApi.subclasses[0]().post_start()


@router.get(
    "/workflows",
    responses={
        200: {"model": WorkflowPage, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["workflows"],
    summary="List workflow executions",
    response_model_by_alias=True,
)
async def get_workflows(
    type: Annotated[Optional[StrictStr], Field(description="A workflow type to list only.")] = Query(None, description="A workflow type to list only.", alias="type"),
    page_token: Annotated[Optional[StrictStr], Field(description="next_page_token of the last page.")] = Query(None, description="next_page_token of the last page.", alias="page_token"),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> WorkflowPage:
    """Newest first, 50 per page."""
    if not BaseWorkflowsApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseWorkflowsApi.subclasses[0]().get_workflows(type, page_token)
