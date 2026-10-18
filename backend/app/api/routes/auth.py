import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.api.errors import http_error
from app.auth import storage
from app.auth.limits import AttemptLimiter, SignInBusy, check_password, new_password_hash
from app.auth.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, UNKNOWN_USER_HASH
from app.config import AUTH_LOGIN_ATTEMPTS_PER_MINUTE, AUTH_TRUSTED_PROXY_HOPS
from app.auth.tokens import (
    AuthNotConfigured, InvalidToken, check_configured, issue_token, read_token,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth")
# The browser keeps the token here, out of reach of page scripts, so that plain download
# links are authenticated too. API clients send it as a bearer token instead.
COOKIE = "access_token"
login_attempts = AttemptLimiter(AUTH_LOGIN_ATTEMPTS_PER_MINUTE)


class Credentials(BaseModel):
    # Unconstrained: a validation error would echo the password back.
    login: str
    password: str


async def require_user(request: Request) -> dict:
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


def _limit_attempts(request: Request) -> None:
    wait = login_attempts.retry_after(_client_address(request))
    if wait is not None:
        raise HTTPException(429, f"Too many sign-in attempts. Try again in {wait} seconds.",
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


def _start_session(request: Request, response: Response, user: dict, admin: bool) -> dict:
    token, expires = issue_token(user, admin)
    # Strict: the cookie never accompanies a request another site initiates.
    response.set_cookie(
        COOKIE, token, expires=expires, httponly=True, samesite="strict", path="/",
        secure="https" in (request.url.scheme, request.headers.get("x-forwarded-proto")))
    return {"access_token": token, "token_type": "bearer",
            **_profile({**user, "expires_at": expires, "admin": admin})}


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
        return _start_session(request, response, user, admin)
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
        return _start_session(request, response, user, True)
    except Exception as exc:
        raise _auth_error(exc) from None


@router.post("/logout", status_code=204)
async def logout():
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
    return response


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
