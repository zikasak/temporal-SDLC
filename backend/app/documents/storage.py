"""Document lifetime and on-disk locations for original uploads."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import DOCUMENT_STORAGE_DIR

DOCUMENT_LIFETIME = timedelta(days=1, minutes=8)


def new_expiry() -> datetime:
    return datetime.now(timezone.utc) + DOCUMENT_LIFETIME


def storage_path(storage_name: str) -> Path:
    return DOCUMENT_STORAGE_DIR / storage_name
