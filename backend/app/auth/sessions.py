"""Server-side sign-in sessions and their rotating refresh tokens.

A session starts at sign-in or registration and ends when it is revoked (sign-out or a
reused refresh token), when it is idle for longer than AUTH_SESSION_IDLE_DAYS or when
AUTH_SESSION_MAX_DAYS have passed since it started. Access tokens name their session, so
every authenticated request checks that it is still active. Only SHA-256 hashes of
refresh tokens are stored.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import AUTH_SESSION_IDLE_DAYS, AUTH_SESSION_MAX_DAYS
from app.db import connect

# How long a just-replaced refresh token is still accepted, so that a refresh whose
# response was lost, or two tabs refreshing at once, do not sign the user out.
REFRESH_GRACE_SECONDS = 30


class RefreshRejected(Exception):
    """A refresh token that cannot renew its session; reason is idle, expired, revoked
    or invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


async def ensure_schema() -> None:
    async with await connect() as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS app")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.auth_sessions (
                id text PRIMARY KEY,
                user_id bigint NOT NULL REFERENCES app.users (id) ON DELETE CASCADE,
                created_at timestamptz NOT NULL DEFAULT now(),
                last_active_at timestamptz NOT NULL DEFAULT now(),
                revoked_at timestamptz
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.auth_refresh_tokens (
                token_hash text PRIMARY KEY,
                session_id text NOT NULL REFERENCES app.auth_sessions (id) ON DELETE CASCADE,
                created_at timestamptz NOT NULL DEFAULT now(),
                replaced_at timestamptz
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS auth_refresh_tokens_session
            ON app.auth_refresh_tokens (session_id)
        """)
        # Sessions nobody can renew any more are kept no longer than their absolute limit.
        await conn.execute(
            "DELETE FROM app.auth_sessions WHERE created_at < now() - %s",
            (timedelta(days=AUTH_SESSION_MAX_DAYS + 1),))


async def start_session(user_id: int) -> tuple[str, str]:
    """A new session of the user: its id and its first refresh token."""
    session_id, token = str(uuid.uuid4()), new_refresh_token()
    async with await connect() as conn:
        await conn.execute("INSERT INTO app.auth_sessions (id, user_id) VALUES (%s, %s)",
                           (session_id, user_id))
        await conn.execute(
            "INSERT INTO app.auth_refresh_tokens (token_hash, session_id) VALUES (%s, %s)",
            (_hash(token), session_id))
    return session_id, token


async def touch_session(session_id: str, user_id: int) -> bool:
    """Record activity on the session; False when it is unknown, revoked or not the user's."""
    async with await connect() as conn:
        cursor = await conn.execute("""
            UPDATE app.auth_sessions SET last_active_at = now()
            WHERE id = %s AND user_id = %s AND revoked_at IS NULL
            RETURNING id
        """, (session_id, user_id))
        return await cursor.fetchone() is not None


async def revoke_session(session_id: str) -> None:
    async with await connect() as conn:
        await conn.execute(
            "UPDATE app.auth_sessions SET revoked_at = now() "
            "WHERE id = %s AND revoked_at IS NULL", (session_id,))


async def revoke_user_sessions(login: str) -> None:
    async with await connect() as conn:
        await conn.execute("""
            UPDATE app.auth_sessions SET revoked_at = now()
            WHERE revoked_at IS NULL
              AND user_id = (SELECT id FROM app.users WHERE login = %s)
        """, (login,))


async def session_of_refresh_token(token: str) -> str | None:
    async with await connect() as conn:
        cursor = await conn.execute(
            "SELECT session_id FROM app.auth_refresh_tokens WHERE token_hash = %s",
            (_hash(token),))
        row = await cursor.fetchone()
        return row["session_id"] if row else None


async def rotate(token: str) -> tuple[dict[str, Any], str]:
    """Exchange a refresh token for a new one: the session (with its user's id and login)
    and the new token. RefreshRejected when the session cannot be renewed; a replaced token
    presented after the grace period revokes the session."""
    new_token = new_refresh_token()
    async with await connect() as conn:
        cursor = await conn.execute("""
            SELECT t.replaced_at, s.id, s.user_id, s.created_at, s.last_active_at,
                   s.revoked_at, u.login, now() AS now
            FROM app.auth_refresh_tokens t
            JOIN app.auth_sessions s ON s.id = t.session_id
            JOIN app.users u ON u.id = s.user_id
            WHERE t.token_hash = %s
            FOR UPDATE OF s
        """, (_hash(token),))
        row = await cursor.fetchone()
        if row is None:
            raise RefreshRejected("invalid")
        if row["revoked_at"] is not None:
            raise RefreshRejected("revoked")
        now: datetime = row["now"]
        if now - row["created_at"] > timedelta(days=AUTH_SESSION_MAX_DAYS):
            raise RefreshRejected("expired")
        if now - row["last_active_at"] > timedelta(days=AUTH_SESSION_IDLE_DAYS):
            raise RefreshRejected("idle")
        replaced = row["replaced_at"]
        if replaced is not None and now - replaced > timedelta(seconds=REFRESH_GRACE_SECONDS):
            # Reuse of a spent token: whoever holds it may have stolen it.
            await conn.execute("UPDATE app.auth_sessions SET revoked_at = now() WHERE id = %s",
                               (row["id"],))
            await conn.commit()
            raise RefreshRejected("revoked")
        # Every token still current is replaced, so one token stays current per session.
        await conn.execute("""
            UPDATE app.auth_refresh_tokens SET replaced_at = now()
            WHERE session_id = %s AND replaced_at IS NULL
        """, (row["id"],))
        await conn.execute(
            "INSERT INTO app.auth_refresh_tokens (token_hash, session_id) VALUES (%s, %s)",
            (_hash(new_token), row["id"]))
        await conn.execute("UPDATE app.auth_sessions SET last_active_at = now() WHERE id = %s",
                           (row["id"],))
        return ({"session_id": row["id"], "id": row["user_id"], "login": row["login"],
                 "created_at": row["created_at"].astimezone(timezone.utc)}, new_token)
