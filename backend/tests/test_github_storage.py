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

TOKEN = "ghs_STORAGE_SENTINEL"
CONNECTION = {"app_id": 123, "app_slug": "my-app", "installation_id": 99, "account": "octocat",
              "bot_user_id": 77}


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
        await conn.execute("DELETE FROM app.github_app_connection")
    yield key
    async with await connect() as conn:
        await conn.execute("DELETE FROM app.github_app_connection")


async def test_connection_routes_persist_encrypt_validate_replace_and_disconnect(
        database, github_http, app_private_key, caplog):
    responses, requests = github_http
    installations = [{"id": 99, "account": {"login": "octocat"}}]
    responses["/app"] = httpx.Response(200, json={"id": 123, "slug": "my-app", "untrusted": TOKEN})
    responses["/app/installations"] = lambda request: httpx.Response(200, json=installations)
    responses["/app/installations/99"] = httpx.Response(200, json=installations[0])
    responses["/app/installations/99/access_tokens"] = httpx.Response(201, json={"token": TOKEN})
    responses["/users/my-app[bot]"] = httpx.Response(200, json={"id": 77})
    credentials = {"app_id": 123, "private_key": app_private_key}
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        initial = await http.get("/connectors/github/connection")
        assert initial.json()["configured"] is False
        connected = await http.put("/connectors/github/connection", json=credentials,
                                   headers={"Origin": "http://localhost:5173"})
        assert connected.status_code == 200
        assert connected.json()["app"] == {"id": 123, "slug": "my-app"}
        assert connected.json()["installation"] == {"id": 99, "account": "octocat"}
        assert TOKEN not in connected.text + caplog.text
        assert "PRIVATE KEY" not in connected.text + caplog.text
        row = await storage.get_connection()
        assert b"PRIVATE KEY" not in bytes(row["encrypted_private_key"])
        assert storage.decrypt_private_key(row) == app_private_key and row["bot_user_id"] == 77
        await storage.ensure_schema()  # Startup schema initialization must not erase saved data.
        restored = await http.get("/connectors/github/connection")
        assert restored.json() == connected.json()

        # A failed replacement keeps the saved connection: rejected credentials, an app
        # installed nowhere, and one installed on several accounts with none named.
        responses["/app"] = httpx.Response(401, text=TOKEN)
        assert (await http.put("/connectors/github/connection", json=credentials)).status_code == 401
        responses["/app"] = httpx.Response(200, json={"id": 123, "slug": "my-app"})
        installations.clear()
        assert (await http.put("/connectors/github/connection", json=credentials)).status_code == 409
        installations.extend([{"id": 98, "account": {"login": "acme"}},
                              {"id": 99, "account": {"login": "octocat"}}])
        several = await http.put("/connectors/github/connection", json=credentials)
        assert several.status_code == 409 and "acme, octocat" in several.text
        missing = await http.put("/connectors/github/connection", json={**credentials, "account": "nobody"})
        assert missing.status_code == 404
        assert (await http.get("/connectors/github/connection")).json() == restored.json()
        named = await http.put("/connectors/github/connection", json={**credentials, "account": "Octocat"})
        assert named.status_code == 200 and named.json()["installation"] == {"id": 99, "account": "octocat"}

        responses["/app/installations/99"] = httpx.Response(
            200, json={"id": 99, "account": {"login": "renamed-org"}})
        checked = await http.post("/connectors/github/connection/check", json={})
        assert checked.status_code == 200 and checked.json()["installation"]["account"] == "renamed-org"
        assert TOKEN not in checked.text + caplog.text
        disconnected = await http.request("DELETE", "/connectors/github/connection", json={})
        assert disconnected.status_code == 204
        assert await storage.get_connection() is None
        assert (await http.get("/connectors/github/repositories")).status_code == 409
    assert all(request.url.host == "api.github.com" for request in requests)


async def test_an_app_created_from_the_manifest_is_saved_and_connected_once_installed(
        database, github_http, app_private_key, caplog):
    responses, requests = github_http
    installations = []
    responses["/app-manifests/v1.code/conversions"] = httpx.Response(201, json={
        "id": 123, "slug": "my-app", "pem": app_private_key, "webhook_secret": None})
    responses["/app-manifests/v1.used/conversions"] = httpx.Response(404)
    responses["/app"] = httpx.Response(200, json={"id": 123, "slug": "my-app"})
    responses["/app/installations"] = lambda request: httpx.Response(200, json=installations)
    responses["/app/installations/99/access_tokens"] = httpx.Response(201, json={"token": TOKEN})
    responses["/users/my-app[bot]"] = httpx.Response(200, json={"id": 77})
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        created = await http.post("/connectors/github/connection/manifest", json={"code": "v1.code"})
        assert created.status_code == 200
        assert created.json()["app"] == {"id": 123, "slug": "my-app"}
        assert created.json()["configured"] is True and created.json()["installation"] is None
        assert "PRIVATE KEY" not in created.text + caplog.text
        # The code is the only credential of the exchange.
        assert requests[0].method == "POST" and "authorization" not in requests[0].headers
        assert storage.decrypt_private_key(await storage.get_connection()) == app_private_key

        # A code GitHub no longer accepts leaves the saved app alone.
        assert (await http.post("/connectors/github/connection/manifest", json={"code": "v1.used"})).status_code == 409
        for path in ("/repositories", "/repositories/octocat/private-project/branches"):
            waiting = await http.get("/connectors/github" + path)
            assert waiting.status_code == 409 and "Install the GitHub App" in waiting.text
        assert (await http.post("/connectors/github/connection/check", json={})).status_code == 409

        installations.append({"id": 99, "account": {"login": "octocat"}})
        installed = await http.post("/connectors/github/connection/check", json={})
        assert installed.status_code == 200
        assert installed.json()["installation"] == {"id": 99, "account": "octocat"}
        assert (await storage.get_connection())["bot_user_id"] == 77

        # Uninstalled and installed again: the next check follows the new installation.
        responses["/app/installations/99"] = httpx.Response(404)
        installations[0] = {"id": 100, "account": {"login": "octocat"}}
        responses["/app/installations/100/access_tokens"] = httpx.Response(201, json={"token": TOKEN})
        again = await http.post("/connectors/github/connection/check", json={})
        assert again.status_code == 200 and again.json()["installation"]["id"] == 100

        # Gone from that account but installed on another: the check does not move there.
        responses["/app/installations/100"] = httpx.Response(404)
        installations[0] = {"id": 101, "account": {"login": "acme"}}
        moved = await http.post("/connectors/github/connection/check", json={})
        assert moved.status_code == 404 and "not installed on octocat" in moved.text
        assert (await http.get("/connectors/github/connection")).json() == again.json()
    assert all(request.url.host == "api.github.com" for request in requests)


async def test_wrong_or_missing_key_is_recoverable_without_erasing_the_connection(database, monkeypatch):
    await storage.save_connection("private key", CONNECTION)
    encrypted = bytes((await storage.get_connection())["encrypted_private_key"])
    monkeypatch.setattr(storage, "GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(GitHubError, match="cannot be decrypted"):
        storage.decrypt_private_key(await storage.get_connection())
    assert bytes((await storage.get_connection())["encrypted_private_key"]) == encrypted
    monkeypatch.setattr(storage, "GITHUB_TOKEN_ENCRYPTION_KEY", database)
    assert storage.decrypt_private_key(await storage.get_connection()) == "private key"
    monkeypatch.setattr(storage, "GITHUB_TOKEN_ENCRYPTION_KEY", "")
    assert storage.metadata(await storage.get_connection())["encryption_ready"] is False
    with pytest.raises(GitHubError, match="ENCRYPTION_KEY"):
        await storage.save_connection("new key", {**CONNECTION, "app_id": 124})
    await storage.disconnect()
    assert await storage.get_connection() is None


async def test_check_cannot_overwrite_a_connection_replaced_while_github_was_responding(database):
    await storage.save_connection("private key", CONNECTION)
    previous = await storage.get_connection()
    await storage.save_connection("new key", {**CONNECTION, "app_id": 124, "app_slug": "new-app"})
    with pytest.raises(GitHubError, match="connection changed"):
        await storage.mark_validated(previous, CONNECTION)
    row = await storage.get_connection()
    assert row["app_slug"] == "new-app" and storage.decrypt_private_key(row) == "new key"


async def test_startup_discards_the_personal_access_token_table(database):
    async with await storage.connect() as conn:
        await conn.execute("CREATE TABLE app.github_connection (encrypted_token bytea)")
    await storage.ensure_schema()
    async with await storage.connect() as conn:
        cursor = await conn.execute("SELECT to_regclass('app.github_connection') AS found")
        assert (await cursor.fetchone())["found"] is None
