"""The shared OpenAI endpoint set on the Settings page: base URL, API key and display
name, used by every model run and coding agent of the openai adapter.

The API key is stored encrypted with SETTINGS_ENCRYPTION_KEY and never leaves the
backend except to the endpoint itself and the coding agents that call it."""
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import SETTINGS_ENCRYPTION_KEY
from app.db import connect

MAX_BASE_URL_CHARS = 2048
MAX_DISPLAY_NAME_CHARS = 100
MAX_API_KEY_CHARS = 8192
# How long the check of a saved endpoint waits for GET {base URL}/models.
CHECK_TIMEOUT_SECONDS = 10
DEFAULT_LABEL = 'OpenAI'


class EndpointError(Exception):
    """A failure of the endpoint's storage or check that is safe to show: its message
    never holds the API key."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class OpenAIEndpoint:
    base_url: str
    api_key: str
    display_name: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or DEFAULT_LABEL


@dataclass(frozen=True)
class StoredEndpoint:
    """The saved row, the key still encrypted."""

    base_url: str
    encrypted_api_key: bytes | None
    display_name: str | None

    @property
    def api_key_set(self) -> bool:
        return self.encrypted_api_key is not None


class OpenAIEndpointInput(BaseModel):
    """The openai_endpoint section of PUT /settings."""

    model_config = ConfigDict(extra='forbid')

    base_url: str = Field(max_length=MAX_BASE_URL_CHARS)
    # None keeps the stored key; '' saves the endpoint without one.
    api_key: str | None = Field(default=None, max_length=MAX_API_KEY_CHARS)
    display_name: str | None = None

    @field_validator('base_url')
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        value = value.strip()
        try:
            url = urlsplit(value)
        except ValueError:
            raise ValueError('Base URL must be an http or https URL') from None
        if (url.scheme not in {'http', 'https'} or not url.hostname
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError('Base URL must be an http or https URL')
        if len(value) > MAX_BASE_URL_CHARS:
            raise ValueError(f'Base URL must be at most {MAX_BASE_URL_CHARS} characters')
        return value

    @field_validator('api_key')
    @classmethod
    def valid_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('API key must not contain control characters')
        return value

    @field_validator('display_name')
    @classmethod
    def valid_display_name(cls, value: str | None) -> str | None:
        value = (value or '').strip()
        if len(value) > MAX_DISPLAY_NAME_CHARS:
            raise ValueError(f'Display name must be at most {MAX_DISPLAY_NAME_CHARS} characters')
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Display name must not contain control characters')
        return value or None


def _cipher() -> Fernet:
    try:
        if not SETTINGS_ENCRYPTION_KEY:
            raise ValueError
        return Fernet(SETTINGS_ENCRYPTION_KEY.encode('ascii'))
    except (ValueError, UnicodeError):
        raise EndpointError(
            503, 'Configure a valid SETTINGS_ENCRYPTION_KEY in the backend.') from None


def encrypt_api_key(api_key: str) -> bytes:
    return _cipher().encrypt(api_key.encode('utf-8'))


def decrypt_api_key(encrypted: bytes) -> str:
    try:
        return _cipher().decrypt(bytes(encrypted)).decode('utf-8')
    except InvalidToken:
        raise EndpointError(
            503, 'The saved OpenAI API key cannot be read with this SETTINGS_ENCRYPTION_KEY. '
            'Restore the key it was saved with, or enter the API key again.') from None


async def ensure_schema(conn) -> None:
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS app.openai_endpoint (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            base_url text NOT NULL,
            -- Null while the endpoint has no key.
            encrypted_api_key bytea,
            display_name text,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    ''')


async def read_stored(conn) -> StoredEndpoint | None:
    row = await (await conn.execute(
        'SELECT base_url, encrypted_api_key, display_name FROM app.openai_endpoint')).fetchone()
    if row is None:
        return None
    return StoredEndpoint(base_url=row['base_url'], display_name=row['display_name'],
                          encrypted_api_key=(None if row['encrypted_api_key'] is None
                                             else bytes(row['encrypted_api_key'])))


async def get_stored_endpoint() -> StoredEndpoint | None:
    async with await connect() as conn:
        return await read_stored(conn)


def resolve(stored: StoredEndpoint | None) -> OpenAIEndpoint | None:
    if stored is None:
        return None
    api_key = '' if stored.encrypted_api_key is None else decrypt_api_key(stored.encrypted_api_key)
    return OpenAIEndpoint(base_url=stored.base_url, api_key=api_key,
                          display_name=stored.display_name)


async def get_openai_endpoint() -> OpenAIEndpoint | None:
    """The saved endpoint with its key decrypted; None while none is saved."""
    return resolve(await get_stored_endpoint())


async def save_endpoint(conn, endpoint: OpenAIEndpoint | None,
                        encrypted_api_key: bytes | None) -> None:
    """Replace or remove the saved endpoint inside the caller's transaction."""
    if endpoint is None:
        await conn.execute('DELETE FROM app.openai_endpoint')
        return
    await conn.execute('''
        INSERT INTO app.openai_endpoint (singleton, base_url, encrypted_api_key, display_name)
        VALUES (true, %s, %s, %s)
        ON CONFLICT (singleton) DO UPDATE SET base_url = EXCLUDED.base_url,
            encrypted_api_key = EXCLUDED.encrypted_api_key,
            display_name = EXCLUDED.display_name, updated_at = now()
    ''', (endpoint.base_url, encrypted_api_key, endpoint.display_name))


def models_url(base_url: str) -> str:
    return f'{base_url.rstrip("/")}/models'


def authorization(api_key: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {api_key}'} if api_key else {}


async def check_endpoint(endpoint: OpenAIEndpoint) -> None:
    """GET {base URL}/models with the key; EndpointError(422) unless it answers
    successfully. The detail names the status or the kind of connection error, never
    the key."""
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_SECONDS) as client:
            response = await client.get(models_url(endpoint.base_url),
                                        headers=authorization(endpoint.api_key))
    except httpx.TimeoutException:
        raise EndpointError(422, (
            f'The OpenAI endpoint did not answer GET /models within {CHECK_TIMEOUT_SECONDS} '
            'seconds. Check the base URL and that the endpoint is reachable from the backend.'
        )) from None
    except (httpx.HTTPError, OSError, ValueError) as exc:
        reason = _redact(str(exc), endpoint.api_key) or type(exc).__name__
        raise EndpointError(422, (
            f'Could not connect to the OpenAI endpoint ({type(exc).__name__}: {reason}). '
            'Check the base URL and that the endpoint is reachable from the backend.')) from None
    if not response.is_success:
        raise EndpointError(422, (
            f'The OpenAI endpoint answered HTTP {response.status_code} to GET /models. '
            'Check the base URL and the API key.'))


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, '[redacted]') if secret else text


def view(stored: StoredEndpoint | None, *, admin: bool) -> dict[str, Any]:
    """The openai_endpoint section of GET /settings for the caller's role."""
    display_name = stored.display_name if stored else None
    if not admin:
        return {'display_name': display_name}
    return {'base_url': stored.base_url if stored else None, 'display_name': display_name,
            'api_key_set': bool(stored and stored.api_key_set)}
