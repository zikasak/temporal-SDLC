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
        # The personal access token this connector used before GitHub Apps.
        await conn.execute("DROP TABLE IF EXISTS app.github_connection")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.github_app_connection (
                singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                app_id bigint NOT NULL,
                app_slug text NOT NULL,
                encrypted_private_key bytea NOT NULL,
                -- Null until the app is installed.
                installation_id bigint,
                account text,
                bot_user_id bigint,
                validated_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
        """)


async def get_connection() -> dict[str, Any] | None:
    async with _connection() as conn:
        cursor = await conn.execute("SELECT * FROM app.github_app_connection WHERE singleton")
        return await cursor.fetchone()


def metadata(row: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "configured": row is not None,
        "encryption_ready": encryption_ready(),
        "app": {"id": row["app_id"], "slug": row["app_slug"]} if row else None,
        "installation": {"id": row["installation_id"], "account": row["account"]}
        if row and row["installation_id"] is not None else None,
        "validated_at": row["validated_at"] if row else None,
    }


def decrypt_private_key(row: dict[str, Any] | None) -> str:
    if row is None:
        raise GitHubError(409, "Connect GitHub before choosing a repository.")
    cipher = _cipher()
    try:
        return cipher.decrypt(bytes(row["encrypted_private_key"])).decode("utf-8")
    except (InvalidToken, UnicodeError):
        raise GitHubError(503, "The saved GitHub App key cannot be decrypted. Restore the original encryption key or disconnect and reconnect.") from None


RETURNED = "app_id, app_slug, installation_id, account, validated_at"


async def save_connection(private_key: str, connection: dict[str, Any]) -> dict[str, Any]:
    encrypted = _cipher().encrypt(private_key.encode("utf-8"))
    async with _connection() as conn:
        cursor = await conn.execute(f"""
            INSERT INTO app.github_app_connection
                (app_id, app_slug, encrypted_private_key, installation_id, account, bot_user_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (singleton) DO UPDATE SET
                app_id = EXCLUDED.app_id, app_slug = EXCLUDED.app_slug,
                encrypted_private_key = EXCLUDED.encrypted_private_key,
                installation_id = EXCLUDED.installation_id, account = EXCLUDED.account,
                bot_user_id = EXCLUDED.bot_user_id, validated_at = now(), updated_at = now()
            RETURNING {RETURNED}
        """, (connection["app_id"], connection["app_slug"], encrypted,
              connection["installation_id"], connection["account"], connection["bot_user_id"]))
        return metadata(await cursor.fetchone())


async def mark_validated(row: dict[str, Any], connection: dict[str, Any]) -> dict[str, Any]:
    async with _connection() as conn:
        cursor = await conn.execute(f"""
            UPDATE app.github_app_connection
            SET app_slug = %s, installation_id = %s, account = %s, bot_user_id = %s,
                validated_at = now()
            WHERE singleton AND encrypted_private_key = %s
            RETURNING {RETURNED}
        """, (connection["app_slug"], connection["installation_id"], connection["account"],
              connection["bot_user_id"], row["encrypted_private_key"]))
        updated = await cursor.fetchone()
    if updated is None:
        raise GitHubError(409, "The GitHub connection changed. Refresh and try again.")
    return metadata(updated)


async def disconnect() -> None:
    async with _connection() as conn:
        await conn.execute("DELETE FROM app.github_app_connection WHERE singleton")
