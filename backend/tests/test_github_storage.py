"""The connection lifecycle and its encryption against an in-memory stand-in of the
connection table: no database is used."""
# Assertions read optional fields directly: a None there fails the test anyway.
# pyright: reportOptionalSubscript=false

from datetime import datetime, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes.github import router
from app.github import storage
from app.github.errors import GitHubError

TOKEN = "ghs_STORAGE_SENTINEL"
CONNECTION = {"app_id": 123, "app_slug": "my-app", "installation_id": 99, "account": "octocat",
              "bot_user_id": 77}


def use_connection_table(monkeypatch):
    """Replace the single-row connection table with a dict; encryption stays real."""
    table = {}

    def returned(row):
        return {**row, "webhook_secret_set": row["encrypted_webhook_secret"] is not None}

    async def ensure_schema():
        pass

    async def get_connection():
        return returned(table["row"]) if "row" in table else None

    async def save_connection(private_key, connection, webhook_secret=None):
        cipher = storage._cipher()
        table["row"] = {
            "app_id": connection["app_id"], "app_slug": connection["app_slug"],
            "encrypted_private_key": cipher.encrypt(private_key.encode("utf-8")),
            "installation_id": connection["installation_id"], "account": connection["account"],
            "bot_user_id": connection["bot_user_id"],
            "encrypted_webhook_secret": (cipher.encrypt(webhook_secret.encode("utf-8"))
                                         if webhook_secret else None),
            "manifest_created": bool(connection.get("manifest_created")),
            "webhook_events_missing": bool(connection.get("webhook_events_missing")),
            "validated_at": datetime.now(timezone.utc)}
        return storage.metadata(returned(table["row"]))

    async def mark_validated(row, connection):
        current = table.get("row")
        if current is None or current["encrypted_private_key"] != row["encrypted_private_key"]:
            raise GitHubError(409, "The GitHub connection changed. Refresh and try again.")
        current.update({key: connection[key] for key in
                        ("app_slug", "installation_id", "account", "bot_user_id")},
                       webhook_events_missing=bool(connection.get("webhook_events_missing")),
                       validated_at=datetime.now(timezone.utc))
        if connection.get("manifest_created") is not None:
            current["manifest_created"] = connection["manifest_created"]
        return storage.metadata(returned(current))

    async def initialize_webhook_secret(row, configure):
        current = table.get("row")
        if current is None or current["encrypted_private_key"] != row["encrypted_private_key"]:
            raise GitHubError(409, "The GitHub connection changed. Refresh and try again.")
        if current["encrypted_webhook_secret"] is not None:
            return False
        secret = await configure()
        current["encrypted_webhook_secret"] = storage._cipher().encrypt(secret.encode("utf-8"))
        return True

    async def disconnect():
        table.pop("row", None)

    for function in (ensure_schema, get_connection, save_connection, mark_validated,
                     initialize_webhook_secret, disconnect):
        monkeypatch.setattr(storage, function.__name__, function)
    return table


@pytest.fixture
def database(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(storage, "GITHUB_TOKEN_ENCRYPTION_KEY", key)
    use_connection_table(monkeypatch)
    return key


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
    # The app has no webhook secret yet: the first check turns its webhook on with one.
    responses["/app/hook/config"] = httpx.Response(200, json={})
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
        assert installed.json()["webhook_secret_set"] is True

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
    assert previous is not None
    await storage.save_connection("new key", {**CONNECTION, "app_id": 124, "app_slug": "new-app"})
    with pytest.raises(GitHubError, match="connection changed"):
        await storage.mark_validated(previous, CONNECTION)
    row = await storage.get_connection()
    assert row["app_slug"] == "new-app" and storage.decrypt_private_key(row) == "new key"
