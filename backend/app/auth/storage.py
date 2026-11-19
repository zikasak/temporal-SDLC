"""User accounts. A login is a username or an email address, compared case-insensitively."""

import re
from typing import Any

from app.db import connect

# An email address or a plain username; whitespace and control characters never match.
_LOGIN = re.compile(r"[a-z0-9._%+-]+(@[a-z0-9-]+(\.[a-z0-9-]+)+)?")
MAX_LOGIN_LENGTH = 254


class InvalidLogin(ValueError):
    pass


def normalize_login(login: str) -> str:
    login = login.strip().lower()
    if not 3 <= len(login) <= MAX_LOGIN_LENGTH or not _LOGIN.fullmatch(login):
        raise InvalidLogin("Use an email address, or a username of letters, digits "
                           "and . _ % + - (3 to 254 characters).")
    return login


async def ensure_schema() -> None:
    async with await connect() as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS app")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.users (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                login text NOT NULL UNIQUE,
                password_hash text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
        """)


async def get_user(login: str) -> dict[str, Any] | None:
    async with await connect() as conn:
        cursor = await conn.execute("SELECT * FROM app.users WHERE login = %s", (login,))
        return await cursor.fetchone()


async def first_user_id() -> int | None:
    """The earliest account still present, which registers the others; None without users."""
    async with await connect() as conn:
        cursor = await conn.execute("SELECT id FROM app.users ORDER BY id LIMIT 1")
        row = await cursor.fetchone()
        return row["id"] if row else None


async def create_user(login: str, password_hash: str, *,
                      only_first: bool = False) -> dict[str, Any] | None:
    """Create the user. None when the login is taken or, with only_first, when an account
    already exists."""
    async with await connect() as conn:
        if only_first:
            # Held until commit: concurrent first registrations cannot both see no users.
            await conn.execute("LOCK TABLE app.users IN SHARE ROW EXCLUSIVE MODE")
            if await (await conn.execute("SELECT 1 FROM app.users LIMIT 1")).fetchone():
                return None
        cursor = await conn.execute("""
            INSERT INTO app.users (login, password_hash) VALUES (%s, %s)
            ON CONFLICT (login) DO NOTHING
            RETURNING *
        """, (login, password_hash))
        return await cursor.fetchone()


async def save_user(login: str, password_hash: str) -> bool:
    """Create the user or replace their password; True when the user is new."""
    async with await connect() as conn:
        cursor = await conn.execute("""
            INSERT INTO app.users (login, password_hash) VALUES (%s, %s)
            ON CONFLICT (login) DO UPDATE SET
                password_hash = EXCLUDED.password_hash, updated_at = now()
            RETURNING created_at = updated_at AS created
        """, (login, password_hash))
        row = await cursor.fetchone()
        assert row is not None  # the upsert returns its row
        return row["created"]


async def remove_user(login: str) -> bool:
    async with await connect() as conn:
        cursor = await conn.execute("DELETE FROM app.users WHERE login = %s", (login,))
        return cursor.rowcount > 0


async def list_users() -> list[dict[str, Any]]:
    async with await connect() as conn:
        cursor = await conn.execute("SELECT id, login, created_at FROM app.users ORDER BY login")
        return await cursor.fetchall()
