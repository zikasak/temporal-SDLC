import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.errors import http_error
from app.auth import sessions, storage
from app.auth.limits import AttemptLimiter, SignInBusy, check_password, new_password_hash
from app.auth.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, UNKNOWN_USER_HASH
from app.config import (
    AUTH_LOGIN_ATTEMPTS_PER_MINUTE, AUTH_REFRESH_ATTEMPTS_PER_MINUTE, AUTH_SESSION_MAX_DAYS,
    AUTH_TRUSTED_PROXY_HOPS,
)
from app.auth.tokens import (
    AuthNotConfigured, InvalidToken, check_configured, issue_token, read_token,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth")
# The browser keeps the token here, out of reach of page scripts, so that plain download
# links are authenticated too. API clients send it as a bearer token instead.
COOKIE = "access_token"
# The refresh token's cookie only accompanies refreshes, at the path the web client
# reaches the API under.
REFRESH_COOKIE = "refresh_token"
REFRESH_COOKIE_PATH = "/api/auth/refresh"
login_attempts = AttemptLimiter(AUTH_LOGIN_ATTEMPTS_PER_MINUTE)
refresh_attempts = AttemptLimiter(AUTH_REFRESH_ATTEMPTS_PER_MINUTE)

_REJECTIONS = {
    "idle": "Your session ended after a period of inactivity. Sign in again.",
    "expired": "Your session expired. Sign in again.",
    "revoked": "Your session was signed out. Sign in again.",
    "invalid": "Sign in to continue.",
}


class Credentials(BaseModel):
    # Unconstrained: a validation error would echo the password back.
    login: str
    password: str


class RefreshInput(BaseModel):
    refresh_token: str | None = None


def _access_token(request: Request) -> str | None:
    scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
    return bearer.strip() if scheme.lower() == "bearer" else request.cookies.get(COOKIE)


async def require_user(request: Request) -> dict:
    """Dependency: the signed-in user, from a bearer token or the session cookie, whose
    session is still active. Records the session's activity."""
    token = _access_token(request)
    try:
        if not token:
            raise InvalidToken
        user = read_token(token)
        if not await sessions.touch_session(user["session_id"], user["id"]):
            raise InvalidToken
        return user
    except AuthNotConfigured as exc:
        raise HTTPException(503, str(exc)) from None
    except InvalidToken:
        raise HTTPException(401, "Sign in to continue.",
                            headers={"WWW-Authenticate": "Bearer"}) from None
    except Exception as exc:
        raise http_error(exc) from None


async def require_first_user(user: dict = Depends(require_user)) -> dict:
    """Dependency: the first user, by the database rather than the token's own claim."""
    try:
        first = await storage.first_user_id()
    except Exception as exc:
        raise http_error(exc) from None
    if user["id"] != first:
        raise HTTPException(403, "Only the first user can register accounts.")
    return user


def _client_address(request: Request) -> str:
    # Each trusted proxy appends the address it saw; anything further left is the
    # caller's own claim.
    chain = [hop.strip() for hop in request.headers.get("x-forwarded-for", "").split(",")
             if hop.strip()] if AUTH_TRUSTED_PROXY_HOPS else []
    chain.append(request.client.host if request.client else "unknown")
    return chain[max(len(chain) - 1 - AUTH_TRUSTED_PROXY_HOPS, 0)]


def _profile(user: dict) -> dict:
    # admin: this user is the first one, who registers the others.
    return {"login": user["login"], "expires_at": user["expires_at"], "admin": user["admin"]}


def _limit_attempts(request: Request, limiter: AttemptLimiter | None = None,
                    what: str = "sign-in") -> None:
    wait = (limiter or login_attempts).retry_after(_client_address(request))
    if wait is not None:
        raise HTTPException(429, f"Too many {what} attempts. Try again in {wait} seconds.",
                            headers={"Retry-After": str(wait)})


def _new_account(body: Credentials) -> tuple[str, str]:
    try:
        login = storage.normalize_login(body.login)
    except storage.InvalidLogin as exc:
        raise HTTPException(422, str(exc)) from None
    if not MIN_PASSWORD_LENGTH <= len(body.password) <= MAX_PASSWORD_LENGTH:
        raise HTTPException(422, f"Use a password of {MIN_PASSWORD_LENGTH} to "
                                 f"{MAX_PASSWORD_LENGTH} characters.")
    return login, body.password


def _issue(request: Request, response: Response, user: dict, admin: bool, session_id: str,
           refresh_token: str, started: datetime | None = None) -> dict:
    """The Session body for a new access token of the session, with both cookies set."""
    token, expires = issue_token(user, session_id, admin)
    secure = "https" in (request.url.scheme, request.headers.get("x-forwarded-proto"))
    # Strict: the cookies never accompany a request another site initiates.
    response.set_cookie(COOKIE, token, expires=expires, httponly=True, samesite="strict",
                        path="/", secure=secure)
    refresh_expires = (started or datetime.now(timezone.utc)) + timedelta(
        days=AUTH_SESSION_MAX_DAYS)
    response.set_cookie(REFRESH_COOKIE, refresh_token, expires=refresh_expires, httponly=True,
                        samesite="strict", path=REFRESH_COOKIE_PATH, secure=secure)
    return {"access_token": token, "token_type": "bearer",
            **_profile({**user, "expires_at": expires, "admin": admin}),
            "refresh_token": refresh_token}


async def _start_session(request: Request, response: Response, user: dict,
                         admin: bool) -> dict:
    session_id, refresh_token = await sessions.start_session(user["id"])
    return _issue(request, response, user, admin, session_id, refresh_token)


def _clear_cookies(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, httponly=True,
                           samesite="strict")


def _auth_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, SignInBusy):
        return HTTPException(503, "Sign-in is busy. Try again in a moment.",
                             headers={"Retry-After": "2"})
    if isinstance(exc, AuthNotConfigured):
        return HTTPException(503, str(exc))
    return http_error(exc)


@router.post("/login")
async def login(body: Credentials, request: Request, response: Response):
    # Before any database or hashing work is spent on the caller.
    _limit_attempts(request)
    try:
        try:
            user = await storage.get_user(storage.normalize_login(body.login))
        except storage.InvalidLogin:
            user = None
        known = len(body.password) <= MAX_PASSWORD_LENGTH and await check_password(
            body.password, user["password_hash"] if user else UNKNOWN_USER_HASH)
        if not user or not known:
            logger.warning("Failed sign-in attempt")
            raise HTTPException(401, "Incorrect login or password.")
        check_configured()
        admin = user["id"] == await storage.first_user_id()
        return await _start_session(request, response, user, admin)
    except Exception as exc:
        raise _auth_error(exc) from None


@router.get("/registration")
async def registration():
    """Whether the first account can still be registered without signing in."""
    try:
        return {"open": await storage.first_user_id() is None}
    except Exception as exc:
        raise http_error(exc) from None


@router.post("/register", status_code=201)
async def register(body: Credentials, request: Request, response: Response):
    """Register the first account and sign it in. Closed once any account exists."""
    _limit_attempts(request)
    closed = HTTPException(403, "Registration is closed. Ask the first user for an account.")
    try:
        # Before hashing, so a closed registration costs the caller's attempt nothing more.
        if await storage.first_user_id() is not None:
            raise closed
        login, password = _new_account(body)
        check_configured()
        user = await storage.create_user(login, await new_password_hash(password),
                                         only_first=True)
        if not user:
            raise closed
        logger.info("Registered the first user")
        return await _start_session(request, response, user, True)
    except Exception as exc:
        raise _auth_error(exc) from None


@router.post("/logout", status_code=204)
async def logout(request: Request, body: RefreshInput | None = Body(None)):
    """Revoke the session of the access token, or else of the refresh token in the body,
    and clear both cookies. 204 signed in or not."""
    session_id = None
    token = _access_token(request)
    try:
        if token:
            try:
                session_id = read_token(token)["session_id"]
            except (InvalidToken, AuthNotConfigured):
                session_id = None
        if session_id is None and body is not None and body.refresh_token:
            session_id = await sessions.session_of_refresh_token(body.refresh_token)
        if session_id is None and token:
            # Expired but signed here: signing out still ends the session it names.
            try:
                session_id = read_token(token, verify_expiry=False)["session_id"]
            except (InvalidToken, AuthNotConfigured):
                session_id = None
        if session_id is not None:
            await sessions.revoke_session(session_id)
    except Exception as exc:
        raise http_error(exc) from None
    response = Response(status_code=204)
    _clear_cookies(response)
    return response


def _refresh_rejected(reason: str) -> JSONResponse:
    response = JSONResponse(status_code=401, headers={"WWW-Authenticate": "Bearer"},
                            content={"detail": _REJECTIONS[reason], "reason": reason})
    # A cookie that can no longer renew anything is not sent again.
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, httponly=True,
                           samesite="strict")
    return response


@router.post("/refresh")
async def refresh(request: Request, response: Response,
                  body: RefreshInput | None = Body(None)):
    """Exchange a refresh token, from the body or the cookie, for a new access token and
    a new refresh token. Needs no access token."""
    try:
        _limit_attempts(request, refresh_attempts, "refresh")
        check_configured()
        token = (body.refresh_token if body is not None and body.refresh_token
                 else request.cookies.get(REFRESH_COOKIE))
        if not token:
            return _refresh_rejected("invalid")
        try:
            session, new_token = await sessions.rotate(token)
        except sessions.RefreshRejected as exc:
            if exc.reason == "revoked":
                logger.warning("Refresh rejected: the session was revoked")
            return _refresh_rejected(exc.reason)
        admin = session["id"] == await storage.first_user_id()
        return _issue(request, response, session, admin, session["session_id"], new_token,
                      session["created_at"])
    except Exception as exc:
        raise _auth_error(exc) from None


@router.get("/me")
async def me(user: dict = Depends(require_user)):
    return _profile(user)


@router.get("/users")
async def users(_: dict = Depends(require_first_user)):
    try:
        return [{"login": user["login"], "created_at": user["created_at"]}
                for user in await storage.list_users()]
    except Exception as exc:
        raise http_error(exc) from None


@router.post("/users", status_code=201)
async def create_user(body: Credentials, _: dict = Depends(require_first_user)):
    try:
        login, password = _new_account(body)
        user = await storage.create_user(login, await new_password_hash(password))
        if not user:
            raise HTTPException(409, f"{login} is already registered.")
        return {"login": user["login"], "created_at": user["created_at"]}
    except Exception as exc:
        raise _auth_error(exc) from None
