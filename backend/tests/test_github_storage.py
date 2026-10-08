"""Real PostgreSQL/crypto lifecycle; never uses the default application database."""

import os

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from app.api.routes.github import router
from app.github import storage
from app.github.errors import GitHubError

TOKEN = "github_pat_STORAGE_SENTINEL"


@pytest.fixture
async def database(monkeypatch):
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database named github_test_*")
    if not conninfo_to_dict(dsn).get("dbname", "").startswith("github_test_"):
        pytest.fail("Refusing storage tests: the disposable database name must start with github_test_")

    async def connect():
        return await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row)

    monkeypatch.setattr(storage, "connect", connect)
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(storage, "GITHUB_TOKEN_ENCRYPTION_KEY", key)
    await storage.ensure_schema()
    async with await connect() as conn:
        await conn.execute("DELETE FROM app.github_connection")
    yield key
    async with await connect() as conn:
        await conn.execute("DELETE FROM app.github_connection")


async def test_connection_routes_persist_encrypt_validate_replace_and_disconnect(database, github_http, caplog):
    responses, requests = github_http
    responses["/user"] = httpx.Response(200, json={"id": 7, "login": "octocat", "untrusted": TOKEN})
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        initial = await http.get("/connectors/github/connection")
        assert initial.json()["configured"] is False
        connected = await http.put("/connectors/github/connection", json={"token": TOKEN},
                                   headers={"Origin": "http://localhost:5173"})
        assert connected.status_code == 200
        assert connected.json()["account"] == {"id": 7, "login": "octocat"}
        assert TOKEN not in connected.text + caplog.text
        row = await storage.get_connection()
        assert TOKEN.encode() not in bytes(row["encrypted_token"])
        assert storage.decrypt_token(row) == TOKEN
        await storage.ensure_schema()  # Startup schema initialization must not erase saved data.
        restored = await http.get("/connectors/github/connection")
        assert restored.json() == connected.json()

        responses["/user"] = httpx.Response(401, text=TOKEN)
        invalid = await http.put("/connectors/github/connection", json={"token": "invalid-replacement"})
        assert invalid.status_code == 401
        assert storage.decrypt_token(await storage.get_connection()) == TOKEN
        assert (await http.get("/connectors/github/connection")).json() == restored.json()

        responses["/user"] = httpx.Response(200, json={"id": 7, "login": "renamed-user"})
        checked = await http.post("/connectors/github/connection/check", json={})
        assert checked.status_code == 200 and checked.json()["account"]["login"] == "renamed-user"
        assert TOKEN not in checked.text + caplog.text
        disconnected = await http.request("DELETE", "/connectors/github/connection", json={})
        assert disconnected.status_code == 204
        assert await storage.get_connection() is None
        assert (await http.get("/connectors/github/repositories")).status_code == 409
    assert all(request.url.host == "api.github.com" for request in requests)


async def test_wrong_or_missing_key_is_recoverable_without_erasing_the_connection(database, monkeypatch):
    await storage.save_connection(TOKEN, {"id": 7, "login": "octocat"})
    encrypted = bytes((await storage.get_connection())["encrypted_token"])
    monkeypatch.setattr(storage, "GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(GitHubError, match="cannot be decrypted"):
        storage.decrypt_token(await storage.get_connection())
    assert bytes((await storage.get_connection())["encrypted_token"]) == encrypted
    monkeypatch.setattr(storage, "GITHUB_TOKEN_ENCRYPTION_KEY", database)
    assert storage.decrypt_token(await storage.get_connection()) == TOKEN
    monkeypatch.setattr(storage, "GITHUB_TOKEN_ENCRYPTION_KEY", "")
    assert storage.metadata(await storage.get_connection())["encryption_ready"] is False
    with pytest.raises(GitHubError, match="ENCRYPTION_KEY"):
        await storage.save_connection("new-token", {"id": 8, "login": "someone"})
    await storage.disconnect()
    assert await storage.get_connection() is None


async def test_check_cannot_overwrite_a_connection_replaced_while_github_was_responding(database):
    await storage.save_connection(TOKEN, {"id": 7, "login": "octocat"})
    previous = await storage.get_connection()
    await storage.save_connection("new-token", {"id": 8, "login": "new-account"})
    with pytest.raises(GitHubError, match="connection changed"):
        await storage.mark_validated(previous, {"id": 7, "login": "old-account"})
    row = await storage.get_connection()
    assert row["login"] == "new-account" and storage.decrypt_token(row) == "new-token"
