"""Connector operations stay outside workflows; sessions retain only metadata."""

from typing import Any

from app.github import client, storage
from app.github.errors import GitHubError
from app.github.models import RepositorySelection

LISTED_ACCOUNTS = 10


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


async def connect(app_id: int, private_key: str, account: str | None = None) -> dict:
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
    })


async def connect_created_app(code: str) -> dict:
    """Save the app GitHub just created from this instance's manifest. It is not installed
    anywhere yet: checking the connection picks its installation up."""
    if not storage.encryption_ready():
        raise GitHubError(503, "Configure a valid GITHUB_TOKEN_ENCRYPTION_KEY in the backend.")
    app = await client.convert_manifest(code)
    # Proves the key before it replaces a working connection.
    client.app_token(app["id"], app["private_key"])
    return await storage.save_connection(app["private_key"], {
        "app_id": app["id"], "app_slug": app["slug"], "installation_id": None,
        "account": None, "bot_user_id": None,
    })


def _app_token(row: dict[str, Any] | None) -> str:
    private_key = storage.decrypt_private_key(row)
    return client.app_token(row["app_id"], private_key)


async def check_connection() -> dict:
    row = await storage.get_connection()
    app_token = _app_token(row)
    app = await client.get_app(app_token)
    installation = None
    if row["installation_id"] is not None:
        try:
            installation = await client.get_installation(app_token, row["installation_id"])
        except GitHubError as exc:
            if exc.status_code != 409:
                raise
    # Not installed when it was saved, or installed again since.
    installation = installation or await _choose_installation(app_token, None)
    token = await client.installation_token(app_token, installation["id"])
    bot_user_id = row["bot_user_id"] or await client.get_bot_user_id(token, app["slug"])
    return await storage.mark_validated(row, {
        "app_slug": app["slug"], "installation_id": installation["id"],
        "account": installation["account"], "bot_user_id": bot_user_id,
    })


async def installation_token(row: dict[str, Any] | None) -> str:
    """A fresh token for the saved installation; GitHub expires each after an hour."""
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
