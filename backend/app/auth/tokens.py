"""Signed, short-lived access tokens (JWT, HS256) naming their server-side session."""

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.config import AUTH_ACCESS_TOKEN_TTL_MINUTES, AUTH_JWT_SECRET

_ALGORITHM = "HS256"


class AuthNotConfigured(Exception):
    pass


class InvalidToken(Exception):
    pass


def check_configured() -> None:
    """AuthNotConfigured before work is done for a session that could not be issued."""
    _secret()


def _secret() -> str:
    # HS256 needs a key at least as long as its 256-bit digest.
    if len(AUTH_JWT_SECRET) < 32:
        raise AuthNotConfigured("Configure an AUTH_JWT_SECRET of at least 32 characters "
                                "in the backend.")
    return AUTH_JWT_SECRET


def issue_token(user: dict[str, Any], session_id: str,
                admin: bool = False) -> tuple[str, datetime]:
    issued = datetime.now(timezone.utc).replace(microsecond=0)
    expires = issued + timedelta(minutes=AUTH_ACCESS_TOKEN_TTL_MINUTES)
    token = jwt.encode({"sub": str(user["id"]), "login": user["login"], "admin": admin,
                        "sid": session_id, "iat": issued, "exp": expires},
                       _secret(), algorithm=_ALGORITHM)
    return token, expires


def read_token(token: str, *, verify_expiry: bool = True) -> dict[str, Any]:
    """The signed-in user a token names, or InvalidToken if it is forged, expired or has no
    session id (as every token issued before sessions were tracked)."""
    secret = _secret()
    try:
        claims = jwt.decode(token, secret, algorithms=[_ALGORITHM],
                            options={"require": ["exp", "sub", "login", "sid"],
                                     "verify_exp": verify_expiry})
        if not isinstance(claims["sid"], str) or not claims["sid"]:
            raise InvalidToken
        return {"id": int(claims["sub"]), "login": str(claims["login"]),
                "admin": claims.get("admin") is True, "session_id": claims["sid"],
                "expires_at": datetime.fromtimestamp(claims["exp"], timezone.utc)}
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise InvalidToken from None
