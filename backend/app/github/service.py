"""Connector operations stay outside workflows; sessions retain only metadata."""

import secrets
from typing import Any

from app.github import client, storage
from app.github.errors import GitHubError
from app.github.models import RepositorySelection

LISTED_ACCOUNTS = 10
# Where GitHub delivers webhook events to this instance.
EVENTS_PATH = "/connectors/github/events"


async def connection_status() -> dict:
    return storage.metadata(await storage.get_connection())


async def _choose_installation(app_token: str, account: str | None) -> dict[str, Any]:
    installations = await client.list_installations(app_token)
    if account is not None:
        installations = [item for item in installations
                         if item["account"].lower() == account.lower()]
        if not installations:
            raise GitHubError(404, f"The GitHub App is not installed on {account}.")
    if not installations:
        raise GitHubError(409, "Install the GitHub App on an account or organization, then connect again.")
    if len(installations) > 1:
        accounts = ", ".join(item["account"] for item in installations[:LISTED_ACCOUNTS])
        raise GitHubError(409, f"The GitHub App is installed on several accounts. Name one of: {accounts}.")
    return installations[0]


async def connect(app_id: int, private_key: str, account: str | None = None,
                  webhook_secret: str | None = None) -> dict:
    """Connect a manually created app; without `webhook_secret` it is saved without one."""
    if not storage.encryption_ready():
        raise GitHubError(503, "Configure a valid GITHUB_TOKEN_ENCRYPTION_KEY in the backend.")
    app_token = client.app_token(app_id, private_key)
    app = await client.get_app(app_token)
    installation = await _choose_installation(app_token, account)
    token = await client.installation_token(app_token, installation["id"])
    bot_user_id = await client.get_bot_user_id(token, app["slug"])
    return await storage.save_connection(private_key, {
        "app_id": app["id"], "app_slug": app["slug"], "installation_id": installation["id"],
        "account": installation["account"], "bot_user_id": bot_user_id,
        "manifest_created": False,
        "webhook_events_missing": client.events_missing(app, installation),
    }, webhook_secret)


async def connect_created_app(code: str) -> dict:
    """Save the app GitHub just created from this instance's manifest, with the webhook
    secret GitHub returns. It is not installed anywhere yet: checking the connection
    picks its installation up."""
    if not storage.encryption_ready():
        raise GitHubError(503, "Configure a valid GITHUB_TOKEN_ENCRYPTION_KEY in the backend.")
    app = await client.convert_manifest(code)
    # Proves the key before it replaces a working connection.
    client.app_token(app["id"], app["private_key"])
    return await storage.save_connection(app["private_key"], {
        "app_id": app["id"], "app_slug": app["slug"], "installation_id": None,
        "account": None, "bot_user_id": None, "manifest_created": True,
        "webhook_events_missing": client.events_missing(app, None),
    }, app["webhook_secret"])


def _connected(row: dict[str, Any] | None) -> dict[str, Any]:
    """The saved connection; GitHub is not connected without one."""
    if row is None:
        raise GitHubError(409, "Connect GitHub before choosing a repository.")
    return row


def _app_token(row: dict[str, Any] | None) -> str:
    row = _connected(row)
    private_key = storage.decrypt_private_key(row)
    return client.app_token(row["app_id"], private_key)


async def _enable_webhook(app_token: str, events_url: str) -> str:
    """Turn on the webhook of an app created from the manifest before mentions were
    supported, with a new secret, pointing at this instance's events endpoint
    `events_url`: an address the hook had before, such as one of another instance or of
    this one before it moved, is replaced, so that deliveries reach this instance."""
    secret = secrets.token_hex(32)
    await client.update_hook_config(app_token, events_url, secret)
    return secret


async def check_connection(events_url: str | None = None) -> dict:
    """Validate the saved app and pick up its installation. `events_url` is where this
    instance receives webhook deliveries, for an app created from the manifest without
    a webhook secret."""
    row = _connected(await storage.get_connection())
    app_token = _app_token(row)
    app = await client.get_app(app_token)
    installation = None
    if row["installation_id"] is not None:
        try:
            installation = await client.get_installation(app_token, row["installation_id"])
        except GitHubError as exc:
            if exc.status_code != 409:
                raise
    # Not installed when it was saved, or installed again since: on the same account
    # only, so that a check never moves the connection to other repositories.
    installation = installation or await _choose_installation(app_token, row["account"])
    token = await client.installation_token(app_token, installation["id"])
    bot_user_id = row["bot_user_id"] or await client.get_bot_user_id(token, app["slug"])
    # Only a connection created from this instance's manifest has its webhook turned on; a
    # manual app keeps its webhook settings, its secret changing only by connecting again.
    # A connection saved before its origin was recorded (manifest_created is null) has no
    # reliable provenance: an app's homepage or webhook address does not prove that this
    # instance's manifest created it, so it is left unchanged like a manual app. Its owner
    # connects again, with a webhook secret or from the manifest, to handle mentions.
    manifest_created = row.get("manifest_created")
    if (manifest_created is True and row.get("encrypted_webhook_secret") is None
            and events_url):
        # The secret is generated, configured at GitHub and saved under the connection's
        # lock, so that concurrent checks never save another secret than GitHub's.
        await storage.initialize_webhook_secret(
            row, lambda: _enable_webhook(app_token, events_url))
    return await storage.mark_validated(row, {
        "app_slug": app["slug"], "installation_id": installation["id"],
        "account": installation["account"], "bot_user_id": bot_user_id,
        "webhook_events_missing": client.events_missing(app, installation),
        "manifest_created": None,
    })


async def installation_token(row: dict[str, Any] | None) -> str:
    """A fresh token for the saved installation; GitHub expires each after an hour."""
    row = _connected(row)
    app_token = _app_token(row)
    if row["installation_id"] is None:
        raise GitHubError(409, "Install the GitHub App, then check the connection in GitHub settings.")
    return await client.installation_token(app_token, row["installation_id"])


async def _token() -> str:
    return await installation_token(await storage.get_connection())


async def list_repositories(page: int) -> dict:
    return await client.list_repositories(await _token(), page)


async def list_branches(full_name: str, page: int) -> dict:
    client.validate_selection(full_name, "main")
    return await client.list_branches(await _token(), full_name, page)


async def resolve_repository_selection(full_name: str, branch: str) -> RepositorySelection:
    client.validate_selection(full_name, branch)
    return await client.resolve_selection(await _token(), full_name, branch)
