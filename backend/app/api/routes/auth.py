import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.api.errors import http_error
from app.auth import storage
from app.auth.passwords import MAX_PASSWORD_LENGTH, UNKNOWN_USER_HASH, verify_password
from app.auth.tokens import AuthNotConfigured, InvalidToken, issue_token, read_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth")
# The browser keeps the token here, out of reach of page scripts, so that plain download
# links are authenticated too. API clients send it as a bearer token instead.
COOKIE = "access_token"


class Credentials(BaseModel):
    # Unconstrained: a validation error would echo the password back.
    login: str
    password: str


def require_user(request: Request) -> dict:
    """Dependency: the signed-in user, from a bearer token or the session cookie."""
    scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
    token = bearer.strip() if scheme.lower() == "bearer" else request.cookies.get(COOKIE)
    try:
        if not token:
            raise InvalidToken
        return read_token(token)
    except AuthNotConfigured as exc:
        raise HTTPException(503, str(exc)) from None
    except InvalidToken:
        raise HTTPException(401, "Sign in to continue.",
                            headers={"WWW-Authenticate": "Bearer"}) from None


def _profile(user: dict) -> dict:
    return {"login": user["login"], "expires_at": user["expires_at"]}


@router.post("/login")
async def login(body: Credentials, request: Request, response: Response):
    try:
        try:
            user = await storage.get_user(storage.normalize_login(body.login))
        except storage.InvalidLogin:
            user = None
        known = len(body.password) <= MAX_PASSWORD_LENGTH and await asyncio.to_thread(
            verify_password, body.password, user["password_hash"] if user else UNKNOWN_USER_HASH)
        if not user or not known:
            logger.warning("Failed sign-in attempt")
            raise HTTPException(401, "Incorrect login or password.")
        token, expires = issue_token(user)
    except HTTPException:
        raise
    except AuthNotConfigured as exc:
        raise HTTPException(503, str(exc)) from None
    except Exception as exc:
        raise http_error(exc) from None
    # Strict: the cookie never accompanies a request another site initiates.
    response.set_cookie(
        COOKIE, token, expires=expires, httponly=True, samesite="strict", path="/",
        secure="https" in (request.url.scheme, request.headers.get("x-forwarded-proto")))
    return {"access_token": token, "token_type": "bearer",
            **_profile({**user, "expires_at": expires})}


@router.post("/logout", status_code=204)
async def logout():
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
    return response


@router.get("/me")
async def me(user: dict = Depends(require_user)):
    return _profile(user)
