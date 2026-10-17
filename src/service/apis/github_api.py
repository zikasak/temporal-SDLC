# coding: utf-8

from typing import Dict, List  # noqa: F401
import importlib
import pkgutil

from service.apis.github_api_base import BaseGithubApi
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
from pydantic import Field, StrictInt, StrictStr
from typing import Any, Dict, Optional
from typing_extensions import Annotated
from service.models.branch_page import BranchPage
from service.models.error import Error
from service.models.git_hub_app_credentials import GitHubAppCredentials
from service.models.git_hub_connection import GitHubConnection
from service.models.manifest_code import ManifestCode
from service.models.repository_page import RepositoryPage
from service.security_api import get_token_bearer

router = APIRouter()

ns_pkg = service.impl
for _, name, _ in pkgutil.iter_modules(ns_pkg.__path__, ns_pkg.__name__ + "."):
    importlib.import_module(name)


@router.get(
    "/connectors/github/connection",
    responses={
        200: {"model": GitHubConnection, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
    },
    tags=["github"],
    summary="GitHub connection status",
    response_model_by_alias=True,
)
async def get_connectors_github_connection(
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> GitHubConnection:
    """The shared connection, without its private key."""
    if not BaseGithubApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseGithubApi.subclasses[0]().get_connectors_github_connection()


@router.put(
    "/connectors/github/connection",
    responses={
        200: {"model": GitHubConnection, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        403: {"model": Error, "description": "Forbidden"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
        413: {"model": Error, "description": "Request Entity Too Large"},
        415: {"model": Error, "description": "Unsupported Media Type"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["github"],
    summary="Connect a GitHub App",
    response_model_by_alias=True,
)
async def put_connectors_github_connection(
    git_hub_app_credentials: GitHubAppCredentials = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> GitHubConnection:
    """Saves the app after GitHub confirms its key and installation. The request must be application/json and come from an allowed origin, if any."""
    if not BaseGithubApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseGithubApi.subclasses[0]().put_connectors_github_connection(git_hub_app_credentials)


@router.delete(
    "/connectors/github/connection",
    responses={
        204: {"description": "No Content"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        403: {"model": Error, "description": "Forbidden"},
        415: {"model": Error, "description": "Unsupported Media Type"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["github"],
    summary="Disconnect GitHub",
    response_model_by_alias=True,
)
async def delete_connectors_github_connection(
    body: Dict[str, Any] = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> None:
    """Removes the shared connection. The body is an empty JSON object."""
    if not BaseGithubApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseGithubApi.subclasses[0]().delete_connectors_github_connection(body)


@router.post(
    "/connectors/github/connection/check",
    responses={
        200: {"model": GitHubConnection, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        403: {"model": Error, "description": "Forbidden"},
        409: {"model": Error, "description": "Conflict"},
        415: {"model": Error, "description": "Unsupported Media Type"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["github"],
    summary="Check the GitHub connection",
    response_model_by_alias=True,
)
async def post_connectors_github_connection_check(
    body: Dict[str, Any] = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> GitHubConnection:
    """Validates the saved app against GitHub and picks up its installation. The body is an empty JSON object."""
    if not BaseGithubApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseGithubApi.subclasses[0]().post_connectors_github_connection_check(body)


@router.post(
    "/connectors/github/connection/manifest",
    responses={
        200: {"model": GitHubConnection, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        403: {"model": Error, "description": "Forbidden"},
        413: {"model": Error, "description": "Request Entity Too Large"},
        415: {"model": Error, "description": "Unsupported Media Type"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["github"],
    summary="Connect a created GitHub App",
    response_model_by_alias=True,
)
async def post_connectors_github_connection_manifest(
    manifest_code: ManifestCode = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> GitHubConnection:
    """Saves the app GitHub created from this instance&#39;s manifest."""
    if not BaseGithubApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseGithubApi.subclasses[0]().post_connectors_github_connection_manifest(manifest_code)


@router.get(
    "/connectors/github/repositories",
    responses={
        200: {"model": RepositoryPage, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        409: {"model": Error, "description": "Conflict"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["github"],
    summary="List repositories",
    response_model_by_alias=True,
)
async def get_connectors_github_repositories(
    page: Annotated[Optional[StrictInt], Field(description="Page number, 1 to 10000; 1 by default.")] = Query(None, description="Page number, 1 to 10000; 1 by default.", alias="page"),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> RepositoryPage:
    """Repositories the installation can access."""
    if not BaseGithubApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseGithubApi.subclasses[0]().get_connectors_github_repositories(page)


@router.get(
    "/connectors/github/repositories/{owner}/{repo}/branches",
    responses={
        200: {"model": BranchPage, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        404: {"model": Error, "description": "Not Found"},
        409: {"model": Error, "description": "Conflict"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["github"],
    summary="List branches",
    response_model_by_alias=True,
)
async def get_connectors_github_repositories_by_owner_by_repo_branches(
    owner: Annotated[StrictStr, Field(description="Repository owner.")] = Path(..., description="Repository owner."),
    repo: Annotated[StrictStr, Field(description="Repository name.")] = Path(..., description="Repository name."),
    page: Annotated[Optional[StrictInt], Field(description="Page number, 1 to 10000; 1 by default.")] = Query(None, description="Page number, 1 to 10000; 1 by default.", alias="page"),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> BranchPage:
    """Branch names of a repository the installation can access."""
    if not BaseGithubApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseGithubApi.subclasses[0]().get_connectors_github_repositories_by_owner_by_repo_branches(owner, repo, page)
