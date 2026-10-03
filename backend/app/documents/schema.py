from app.config import DOCUMENT_STORAGE_DIR
from app.db import connect


async def ensure_document_schema() -> None:
    DOCUMENT_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    async with await connect() as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS app")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app.documents (
                id uuid PRIMARY KEY,
                filename text NOT NULL,
                content_type text NOT NULL,
                size_bytes bigint NOT NULL,
                sha256 text NOT NULL,
                storage_name text NOT NULL UNIQUE,
                parse_status text NOT NULL DEFAULT 'pending',
                parsed_text text,
                parse_error text,
                origin text NOT NULL DEFAULT 'upload',
                session_id text,
                revision integer NOT NULL DEFAULT 1,
                created_at timestamptz NOT NULL DEFAULT now(),
                expires_at timestamptz NOT NULL,
                CONSTRAINT documents_parse_status CHECK
                    (parse_status IN ('pending', 'parsed', 'failed'))
            )
            """
        )
        await conn.execute(
            "ALTER TABLE app.documents ADD COLUMN IF NOT EXISTS origin text "
            "NOT NULL DEFAULT 'upload'"
        )
        await conn.execute(
            "ALTER TABLE app.documents ADD COLUMN IF NOT EXISTS session_id text"
        )
        await conn.execute(
            "ALTER TABLE app.documents ADD COLUMN IF NOT EXISTS revision integer "
            "NOT NULL DEFAULT 1"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS documents_expires_at ON app.documents (expires_at)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS documents_session_origin "
            "ON app.documents (session_id, origin)"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app.session_passages (
                session_id text NOT NULL,
                document_id uuid NOT NULL REFERENCES app.documents (id) ON DELETE CASCADE,
                "offset" integer NOT NULL,
                text text NOT NULL,
                retrieved_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (session_id, document_id, "offset")
            )
            """
        )
