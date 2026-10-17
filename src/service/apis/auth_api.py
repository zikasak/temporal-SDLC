# coding: utf-8

from typing import Dict, List  # noqa: F401
import importlib
import pkgutil

from service.apis.auth_api_base import BaseAuthApi
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
from typing import Any, List
from service.models.account import Account
from service.models.credentials import Credentials
from service.models.error import Error
from service.models.profile import Profile
from service.models.registration_status import RegistrationStatus
from service.models.session import Session
from service.security_api import get_token_bearer

router = APIRouter()

ns_pkg = service.impl
for _, name, _ in pkgutil.iter_modules(ns_pkg.__path__, ns_pkg.__name__ + "."):
    importlib.import_module(name)


@router.post(
    "/auth/login",
    responses={
        200: {"model": Session, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        422: {"model": Error, "description": "Unprocessable Entity"},
        429: {"model": Error, "description": "Too Many Requests"},
        503: {"model": Error, "description": "Service Unavailable"},
    },
    tags=["auth"],
    summary="Sign in",
    response_model_by_alias=True,
)
async def post_auth_login(
    credentials: Credentials = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> Session:
    """No sign-in required. Returns a session and sets the access_token cookie."""
    if not BaseAuthApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseAuthApi.subclasses[0]().post_auth_login(credentials)


@router.post(
    "/auth/logout",
    responses={
        204: {"description": "No Content"},
    },
    tags=["auth"],
    summary="Sign out",
    response_model_by_alias=True,
)
async def post_auth_logout(
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> None:
    """No sign-in required. Clears the session cookie."""
    if not BaseAuthApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseAuthApi.subclasses[0]().post_auth_logout()


@router.get(
    "/auth/me",
    responses={
        200: {"model": Profile, "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
    },
    tags=["auth"],
    summary="Current user",
    response_model_by_alias=True,
)
async def get_auth_me(
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> Profile:
    """The signed-in user."""
    if not BaseAuthApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseAuthApi.subclasses[0]().get_auth_me()


@router.post(
    "/auth/register",
    responses={
        201: {"model": Session, "description": "Created"},
        403: {"model": Error, "description": "Forbidden"},
        422: {"model": Error, "description": "Unprocessable Entity"},
        429: {"model": Error, "description": "Too Many Requests"},
        503: {"model": Error, "description": "Service Unavailable"},
    },
    tags=["auth"],
    summary="Register the first account",
    response_model_by_alias=True,
)
async def post_auth_register(
    credentials: Credentials = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> Session:
    """No sign-in required. Registers and signs in the first account; 403 once any account exists."""
    if not BaseAuthApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseAuthApi.subclasses[0]().post_auth_register(credentials)


@router.get(
    "/auth/registration",
    responses={
        200: {"model": RegistrationStatus, "description": "OK"},
        503: {"model": Error, "description": "Service Unavailable"},
    },
    tags=["auth"],
    summary="Registration status",
    response_model_by_alias=True,
)
async def get_auth_registration(
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> RegistrationStatus:
    """No sign-in required. Whether the first account can still be registered."""
    if not BaseAuthApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseAuthApi.subclasses[0]().get_auth_registration()


@router.get(
    "/auth/users",
    responses={
        200: {"model": List[Account], "description": "OK"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        403: {"model": Error, "description": "Forbidden"},
    },
    tags=["auth"],
    summary="List accounts",
    response_model_by_alias=True,
)
async def get_auth_users(
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> List[Account]:
    """Administrator only."""
    if not BaseAuthApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseAuthApi.subclasses[0]().get_auth_users()


@router.post(
    "/auth/users",
    responses={
        201: {"model": Account, "description": "Created"},
        401: {"model": Error, "description": "Unauthorized"},
        503: {"model": Error, "description": "Service Unavailable"},
        403: {"model": Error, "description": "Forbidden"},
        409: {"model": Error, "description": "Conflict"},
        422: {"model": Error, "description": "Unprocessable Entity"},
    },
    tags=["auth"],
    summary="Create an account",
    response_model_by_alias=True,
)
async def post_auth_users(
    credentials: Credentials = Body(None, description=""),
    token_bearer: TokenModel = Security(
        get_token_bearer
    ),
) -> Account:
    """Administrator only."""
    if not BaseAuthApi.subclasses:
        raise HTTPException(status_code=500, detail="Not implemented")
    return await BaseAuthApi.subclasses[0]().post_auth_users(credentials)
