"""Persistent app-instance connection. Credentials never leave this backend."""

from contextlib import asynccontextmanager
from typing import Any

import psycopg
from cryptography.fernet import Fernet, InvalidToken

from app.config import GITHUB_TOKEN_ENCRYPTION_KEY
from app.db import connect
from app.github.errors import GitHubError


def _cipher() -> Fernet:
    try:
        if not GITHUB_TOKEN_ENCRYPTION_KEY:
            raise ValueError
        return Fernet(GITHUB_TOKEN_ENCRYPTION_KEY.encode("ascii"))
    except (ValueError, UnicodeError):
        raise GitHubError(503, "Configure a valid GITHUB_TOKEN_ENCRYPTION_KEY in the backend.") from None


def encryption_ready() -> bool:
    try:
        _cipher()
    except GitHubError:
        return False
    return True


@asynccontextmanager
async def _connection():
    # Database errors may contain query parameters. Never log encrypted credentials.
    try:
        async with await connect() as conn:
            yield conn
    except psycopg.Error:
        raise GitHubError(503, "GitHub connection storage is unavailable.") from None


async def ensure_schema() -> None:
    async with _connection() as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS app")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.github_connection (
                singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                encrypted_token bytea NOT NULL,
                user_id bigint NOT NULL,
                login text NOT NULL,
                validated_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
        """)


async def get_connection() -> dict[str, Any] | None:
    async with _connection() as conn:
        cursor = await conn.execute("SELECT * FROM app.github_connection WHERE singleton")
        return await cursor.fetchone()


def metadata(row: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "configured": row is not None,
        "encryption_ready": encryption_ready(),
        "account": {"id": row["user_id"], "login": row["login"]} if row else None,
        "validated_at": row["validated_at"] if row else None,
    }


def decrypt_token(row: dict[str, Any] | None) -> str:
    if row is None:
        raise GitHubError(409, "Connect GitHub before choosing a repository.")
    cipher = _cipher()
    try:
        return cipher.decrypt(bytes(row["encrypted_token"])).decode("utf-8")
    except (InvalidToken, UnicodeError):
        raise GitHubError(503, "The saved GitHub token cannot be decrypted. Restore the original key or disconnect and reconnect.") from None


async def save_connection(token: str, account: dict[str, Any]) -> dict[str, Any]:
    encrypted = _cipher().encrypt(token.encode("utf-8"))
    async with _connection() as conn:
        cursor = await conn.execute("""
            INSERT INTO app.github_connection (encrypted_token, user_id, login)
            VALUES (%s, %s, %s)
            ON CONFLICT (singleton) DO UPDATE SET
                encrypted_token = EXCLUDED.encrypted_token, user_id = EXCLUDED.user_id,
                login = EXCLUDED.login, validated_at = now(), updated_at = now()
            RETURNING user_id, login, validated_at
        """, (encrypted, account["id"], account["login"]))
        return metadata(await cursor.fetchone())


async def mark_validated(row: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    async with _connection() as conn:
        cursor = await conn.execute("""
            UPDATE app.github_connection SET user_id = %s, login = %s, validated_at = now()
            WHERE singleton AND encrypted_token = %s
            RETURNING user_id, login, validated_at
        """, (account["id"], account["login"], row["encrypted_token"]))
        updated = await cursor.fetchone()
    if updated is None:
        raise GitHubError(409, "The GitHub connection changed. Refresh and try again.")
    return metadata(updated)


async def disconnect() -> None:
    async with _connection() as conn:
        await conn.execute("DELETE FROM app.github_connection WHERE singleton")
