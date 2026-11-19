"""Persistent app-instance connection. Credentials never leave this backend."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import psycopg
from psycopg.types.json import Jsonb
from cryptography.fernet import Fernet, InvalidToken

from app.config import GITHUB_TOKEN_ENCRYPTION_KEY
from app.db import connect
from app.github.errors import GitHubError
from app.github.models import PullRequestMention


@dataclass
class PendingMention:
    """A claimed mention that is not handed over to its delivery yet."""

    comment_type: str
    comment_id: int
    session_id: str
    mention: PullRequestMention
    # Its place in the order the delivery's mentions arrived in; None for claims made
    # before the order was recorded.
    seq: int | None = None


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
        # The webhook secret, encrypted like the key; null while the connection has none.
        await conn.execute("""
            ALTER TABLE app.github_app_connection
                ADD COLUMN IF NOT EXISTS encrypted_webhook_secret bytea,
                ADD COLUMN IF NOT EXISTS manifest_created boolean,
                ADD COLUMN IF NOT EXISTS webhook_events_missing boolean NOT NULL DEFAULT false
        """)
        # Whether the app was created from this instance's manifest: null for connections
        # saved before mentions were supported. Such a connection has no reliable
        # provenance, so a check leaves its webhook settings unchanged like a manual one's.
        await conn.execute("""
            ALTER TABLE app.github_app_connection
                ALTER COLUMN manifest_created DROP NOT NULL,
                ALTER COLUMN manifest_created DROP DEFAULT
        """)
        # The pull requests the app opened for deliveries, by repository and number.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.github_pull_requests (
                repository text NOT NULL,
                number bigint NOT NULL,
                session_id text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (repository, number)
            )
        """)
        # Webhook deliveries and comments already handled, so that a redelivery is not.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.github_webhook_deliveries (
                delivery_id text PRIMARY KEY,
                received_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.github_webhook_comments (
                comment_type text NOT NULL,
                comment_id bigint NOT NULL,
                received_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (comment_type, comment_id)
            )
        """)
        # A claimed mention keeps what its delivery is handed until it was handed over:
        # a claim that is not handed over yet, such as one whose API process stopped
        # before Temporal accepted it, is handed over again by a redelivery or the
        # recovery loop. Claims from before this was recorded were all handed over.
        await conn.execute("""
            ALTER TABLE app.github_webhook_comments
                ADD COLUMN IF NOT EXISTS session_id text,
                ADD COLUMN IF NOT EXISTS mention jsonb,
                ADD COLUMN IF NOT EXISTS handed_over boolean NOT NULL DEFAULT true
        """)
        await conn.execute("""
            ALTER TABLE app.github_webhook_deliveries
                ADD COLUMN IF NOT EXISTS comment_type text,
                ADD COLUMN IF NOT EXISTS comment_id bigint
        """)
        # The order mentions arrived in, per delivery: a mention is admitted, taking the
        # next number, as soon as its delivery is known and before GitHub is asked about
        # its author, and its claim keeps that number. Mentions are handed over in that
        # order, so that a later fix request never overtakes an earlier one whose
        # authorisation or handover is still under way or was interrupted.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.github_mention_admissions (
                seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                session_id text NOT NULL,
                comment_type text NOT NULL,
                comment_id bigint NOT NULL,
                admitted_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (comment_type, comment_id)
            )
        """)
        await conn.execute("""
            ALTER TABLE app.github_webhook_comments
                ADD COLUMN IF NOT EXISTS admission_seq bigint
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS github_webhook_comments_pending
            ON app.github_webhook_comments (session_id, admission_seq)
            WHERE NOT handed_over
        """)


async def get_connection() -> dict[str, Any] | None:
    async with _connection() as conn:
        cursor = await conn.execute(
            "SELECT *, encrypted_webhook_secret IS NOT NULL AS webhook_secret_set "
            "FROM app.github_app_connection WHERE singleton")
        return await cursor.fetchone()


def metadata(row: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "configured": row is not None,
        "encryption_ready": encryption_ready(),
        "app": {"id": row["app_id"], "slug": row["app_slug"]} if row else None,
        "installation": {"id": row["installation_id"], "account": row["account"]}
        if row and row["installation_id"] is not None else None,
        "validated_at": row["validated_at"] if row else None,
        "webhook_secret_set": bool(row and row.get("webhook_secret_set")),
        "webhook_events_missing": bool(row and row.get("webhook_events_missing")),
    }


def decrypt_webhook_secret(row: dict[str, Any] | None) -> str | None:
    """The connection's webhook secret; None without a connection or a secret."""
    if row is None or row.get("encrypted_webhook_secret") is None:
        return None
    try:
        return _cipher().decrypt(bytes(row["encrypted_webhook_secret"])).decode("utf-8")
    except (InvalidToken, UnicodeError):
        return None


def decrypt_private_key(row: dict[str, Any] | None) -> str:
    if row is None:
        raise GitHubError(409, "Connect GitHub before choosing a repository.")
    cipher = _cipher()
    try:
        return cipher.decrypt(bytes(row["encrypted_private_key"])).decode("utf-8")
    except (InvalidToken, UnicodeError):
        raise GitHubError(503, "The saved GitHub App key cannot be decrypted. Restore the original encryption key or disconnect and reconnect.") from None


RETURNED = ("app_id, app_slug, installation_id, account, validated_at, webhook_events_missing, "
            "encrypted_webhook_secret IS NOT NULL AS webhook_secret_set")


async def save_connection(private_key: str, connection: dict[str, Any],
                          webhook_secret: str | None = None) -> dict[str, Any]:
    """Replace the connection. Without `webhook_secret` it is saved without one."""
    cipher = _cipher()
    encrypted = cipher.encrypt(private_key.encode("utf-8"))
    encrypted_secret = (cipher.encrypt(webhook_secret.encode("utf-8"))
                        if webhook_secret else None)
    async with _connection() as conn:
        cursor = await conn.execute(f"""
            INSERT INTO app.github_app_connection
                (app_id, app_slug, encrypted_private_key, installation_id, account, bot_user_id,
                 encrypted_webhook_secret, manifest_created, webhook_events_missing)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (singleton) DO UPDATE SET
                app_id = EXCLUDED.app_id, app_slug = EXCLUDED.app_slug,
                encrypted_private_key = EXCLUDED.encrypted_private_key,
                installation_id = EXCLUDED.installation_id, account = EXCLUDED.account,
                bot_user_id = EXCLUDED.bot_user_id,
                encrypted_webhook_secret = EXCLUDED.encrypted_webhook_secret,
                manifest_created = EXCLUDED.manifest_created,
                webhook_events_missing = EXCLUDED.webhook_events_missing,
                validated_at = now(), updated_at = now()
            RETURNING {RETURNED}
        """, (connection["app_id"], connection["app_slug"], encrypted,
              connection["installation_id"], connection["account"], connection["bot_user_id"],
              encrypted_secret, bool(connection.get("manifest_created")),
              bool(connection.get("webhook_events_missing"))))
        return metadata(await cursor.fetchone())


async def mark_validated(row: dict[str, Any], connection: dict[str, Any]) -> dict[str, Any]:
    """Record a successful check. The webhook secret is left as it is: only
    initialize_webhook_secret sets one at a check, under the connection's lock."""
    async with _connection() as conn:
        cursor = await conn.execute(f"""
            UPDATE app.github_app_connection
            SET app_slug = %s, installation_id = %s, account = %s, bot_user_id = %s,
                webhook_events_missing = %s,
                manifest_created = COALESCE(%s, manifest_created),
                validated_at = now()
            WHERE singleton AND encrypted_private_key = %s
            RETURNING {RETURNED}
        """, (connection["app_slug"], connection["installation_id"], connection["account"],
              connection["bot_user_id"], bool(connection.get("webhook_events_missing")),
              connection.get("manifest_created"),
              row["encrypted_private_key"]))
        updated = await cursor.fetchone()
    if updated is None:
        raise GitHubError(409, "The GitHub connection changed. Refresh and try again.")
    return metadata(updated)


async def initialize_webhook_secret(row: dict[str, Any],
                                    configure: Callable[[], Awaitable[str]]) -> bool:
    """Give the connection of `row` a webhook secret while it has none: `configure` sets a
    new secret at GitHub and returns it, and it is saved in the same transaction, which
    holds the connection's row lock throughout. Concurrent calls therefore run one after
    the other: a later one finds the secret saved and leaves GitHub as it is, so the saved
    secret is always the one GitHub signs with. False when a secret was already set."""
    cipher = _cipher()
    async with _connection() as conn:
        async with conn.transaction():
            cursor = await conn.execute("""
                SELECT encrypted_private_key, encrypted_webhook_secret
                FROM app.github_app_connection WHERE singleton FOR UPDATE
            """)
            current = await cursor.fetchone()
            if (current is None or bytes(current["encrypted_private_key"])
                    != bytes(row["encrypted_private_key"])):
                raise GitHubError(409, "The GitHub connection changed. Refresh and try again.")
            if current["encrypted_webhook_secret"] is not None:
                return False
            # A failure here rolls back, saving nothing; the next check sets a new secret.
            secret = await configure()
            await conn.execute("""
                UPDATE app.github_app_connection
                SET encrypted_webhook_secret = %s, updated_at = now()
                WHERE singleton
            """, (cipher.encrypt(secret.encode("utf-8")),))
    return True


async def disconnect() -> None:
    async with _connection() as conn:
        await conn.execute("DELETE FROM app.github_app_connection WHERE singleton")


async def record_pull_request(full_name: str, number: int, session_id: str) -> None:
    """Remember that the app opened pull request `number` of `full_name` for the delivery
    of `session_id`, so that mentions on it find their delivery."""
    async with _connection() as conn:
        await conn.execute("""
            INSERT INTO app.github_pull_requests (repository, number, session_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (repository, number) DO UPDATE SET session_id = EXCLUDED.session_id
        """, (full_name.lower(), number, session_id))


async def pull_request_session(full_name: str, number: int) -> str | None:
    """The session whose delivery the app opened this pull request for, if any."""
    async with _connection() as conn:
        cursor = await conn.execute(
            "SELECT session_id FROM app.github_pull_requests WHERE repository = %s AND number = %s",
            (full_name.lower(), number))
        row = await cursor.fetchone()
    return row["session_id"] if row else None


async def delivery_handled(delivery_id: str) -> bool:
    async with _connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM app.github_webhook_deliveries WHERE delivery_id = %s", (delivery_id,))
        return await cursor.fetchone() is not None


async def record_delivery(delivery_id: str) -> bool:
    """Record a handled webhook delivery; false when it was already recorded."""
    async with _connection() as conn:
        cursor = await conn.execute("""
            INSERT INTO app.github_webhook_deliveries (delivery_id) VALUES (%s)
            ON CONFLICT DO NOTHING RETURNING delivery_id
        """, (delivery_id,))
        return await cursor.fetchone() is not None


async def admit_mention(session_id: str, comment_type: str,
                        comment_id: int) -> tuple[int | None, bool]:
    """Give a mention of the delivery of `session_id` its place in the order mentions
    arrived in: its admission number, and whether this call admitted it (true) or found
    it admitted by a concurrent copy of its delivery (false)."""
    async with _connection() as conn:
        for _ in range(3):
            cursor = await conn.execute("""
                INSERT INTO app.github_mention_admissions (session_id, comment_type, comment_id)
                VALUES (%s, %s, %s) ON CONFLICT (comment_type, comment_id) DO NOTHING
                RETURNING seq
            """, (session_id, comment_type, comment_id))
            row = await cursor.fetchone()
            if row is not None:
                return row["seq"], True
            cursor = await conn.execute("""
                SELECT seq FROM app.github_mention_admissions
                WHERE comment_type = %s AND comment_id = %s
            """, (comment_type, comment_id))
            row = await cursor.fetchone()
            if row is not None:
                return row["seq"], False
    return None, False


async def release_admission(seq: int) -> None:
    """Drop the admission of a mention that was ignored or could not be claimed."""
    async with _connection() as conn:
        await conn.execute("DELETE FROM app.github_mention_admissions WHERE seq = %s", (seq,))


async def purge_admissions(older_than_seconds: int) -> None:
    """Drop admissions whose request stopped before it claimed or released them."""
    async with _connection() as conn:
        await conn.execute("""
            DELETE FROM app.github_mention_admissions
            WHERE admitted_at < now() - make_interval(secs => %s)
        """, (older_than_seconds,))


@asynccontextmanager
async def mention_order_lock(session_id: str, wait_seconds: float):
    """Hold the lock that orders the handovers of the delivery's mentions, across API
    processes, while the body runs; yields false when it could not be taken in time."""
    async with _connection() as conn:
        async with conn.transaction():
            deadline = asyncio.get_running_loop().time() + wait_seconds
            while True:
                cursor = await conn.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS locked",
                    (f"github-mentions:{session_id}",))
                row = await cursor.fetchone()
                locked = bool(row and row["locked"])
                if locked or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.2)
            yield locked


async def claim_mention(comment_type: str, comment_id: int, delivery_id: str | None,
                        session_id: str, mention: PullRequestMention,
                        seq: int | None = None) -> str | None:
    """Claim a comment and its delivery in one transaction, so that neither claim is kept
    without the other, with the mention to hand over to the delivery of `session_id` and
    its admission number `seq`, whose admission the claim takes over. The claim stays
    pending until mark_handed_over. None when both were claimed, otherwise "comment" or
    "delivery" for the one already claimed; nothing is then claimed."""
    try:
        return await _claim_mention(comment_type, comment_id, delivery_id, session_id,
                                    mention, seq)
    except _AlreadyClaimed:
        return "delivery"


async def _claim_mention(comment_type: str, comment_id: int, delivery_id: str | None,
                         session_id: str, mention: PullRequestMention,
                         seq: int | None) -> str | None:
    async with _connection() as conn:
        async with conn.transaction():
            cursor = await conn.execute("""
                INSERT INTO app.github_webhook_comments
                    (comment_type, comment_id, session_id, mention, handed_over, admission_seq)
                VALUES (%s, %s, %s, %s, false, %s) ON CONFLICT DO NOTHING RETURNING comment_id
            """, (comment_type, comment_id, session_id, Jsonb(mention), seq))
            if await cursor.fetchone() is None:
                return "comment"
            if seq is not None:
                await conn.execute(
                    "DELETE FROM app.github_mention_admissions WHERE seq = %s", (seq,))
            if delivery_id:
                cursor = await conn.execute("""
                    INSERT INTO app.github_webhook_deliveries
                        (delivery_id, comment_type, comment_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING RETURNING delivery_id
                """, (delivery_id, comment_type, comment_id))
                if await cursor.fetchone() is None:
                    # Roll the comment's claim back with the transaction.
                    raise _AlreadyClaimed
    return None


class _AlreadyClaimed(Exception):
    pass


def _pending(row: dict[str, Any] | None) -> PendingMention | None:
    if row is None or row["handed_over"] or row["session_id"] is None or row["mention"] is None:
        return None
    return PendingMention(row["comment_type"], row["comment_id"], row["session_id"],
                          cast(PullRequestMention, dict(row["mention"])),
                          row.get("admission_seq"))


async def pending_comment(comment_type: str, comment_id: int) -> PendingMention | None:
    """The claimed mention of this comment while it is not handed over yet."""
    async with _connection() as conn:
        cursor = await conn.execute("""
            SELECT comment_type, comment_id, session_id, mention, handed_over, admission_seq
            FROM app.github_webhook_comments WHERE comment_type = %s AND comment_id = %s
        """, (comment_type, comment_id))
        return _pending(await cursor.fetchone())


async def pending_delivery(delivery_id: str) -> PendingMention | None:
    """The claimed mention a delivery carried while it is not handed over yet."""
    async with _connection() as conn:
        cursor = await conn.execute("""
            SELECT c.comment_type, c.comment_id, c.session_id, c.mention, c.handed_over,
                c.admission_seq
            FROM app.github_webhook_deliveries d
            JOIN app.github_webhook_comments c
                ON c.comment_type = d.comment_type AND c.comment_id = d.comment_id
            WHERE d.delivery_id = %s
        """, (delivery_id,))
        return _pending(await cursor.fetchone())


async def pending_sessions(older_than_seconds: int, limit: int = 100) -> list[str]:
    """The sessions with claimed mentions not handed over yet, claimed at least that long
    ago so that a request still handing one over is left to finish; oldest first."""
    async with _connection() as conn:
        cursor = await conn.execute("""
            SELECT session_id FROM app.github_webhook_comments
            WHERE NOT handed_over AND session_id IS NOT NULL AND mention IS NOT NULL
                AND received_at < now() - make_interval(secs => %s)
            GROUP BY session_id ORDER BY min(received_at) LIMIT %s
        """, (older_than_seconds, limit))
        rows = await cursor.fetchall()
    return [row["session_id"] for row in rows]


async def pending_with_barrier(session_id: str, newer_than_seconds: int
                               ) -> tuple[list[PendingMention], int | None]:
    """The delivery's claimed mentions not handed over yet, in the order they arrived,
    with the admission number of its earliest mention that is admitted but not claimed
    yet while its request may still claim it (None when there is none), both read in one statement and so from one snapshot:
    claiming a mention moves it from the admissions to the claims in one transaction,
    so a mention is always seen either as a barrier or as pending, never as neither."""
    async with _connection() as conn:
        cursor = await conn.execute("""
            WITH barrier AS (
                SELECT min(seq) AS seq FROM app.github_mention_admissions
                WHERE session_id = %s AND admitted_at > now() - make_interval(secs => %s)
            )
            SELECT b.seq AS barrier, c.comment_type, c.comment_id, c.session_id, c.mention,
                c.handed_over, c.admission_seq
            FROM barrier b
            LEFT JOIN app.github_webhook_comments c
                ON NOT c.handed_over AND c.session_id = %s
            ORDER BY c.admission_seq NULLS FIRST, c.received_at, c.comment_id
        """, (session_id, newer_than_seconds, session_id))
        rows = await cursor.fetchall()
    barrier = rows[0]["barrier"] if rows else None
    return [item for item in map(_pending, rows) if item is not None], barrier


async def mark_handed_over(comment_type: str, comment_id: int) -> None:
    """The claimed mention reached its delivery, or has none to reach: it is handled."""
    async with _connection() as conn:
        await conn.execute("""
            UPDATE app.github_webhook_comments SET handed_over = true, mention = NULL
            WHERE comment_type = %s AND comment_id = %s
        """, (comment_type, comment_id))
