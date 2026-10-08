"""Connector operations stay outside workflows; sessions retain only metadata."""

from app.github import client, storage
from app.github.errors import GitHubError
from app.github.models import RepositorySelection


async def connection_status() -> dict:
    return storage.metadata(await storage.get_connection())


async def connect(token: str) -> dict:
    if not storage.encryption_ready():
        raise GitHubError(503, "Configure a valid GITHUB_TOKEN_ENCRYPTION_KEY in the backend.")
    account = await client.get_user(token)
    return await storage.save_connection(token, account)


async def check_connection() -> dict:
    row = await storage.get_connection()
    token = storage.decrypt_token(row)
    account = await client.get_user(token)
    return await storage.mark_validated(row, account)


async def list_repositories(page: int) -> dict:
    token = storage.decrypt_token(await storage.get_connection())
    return await client.list_repositories(token, page)


async def list_branches(full_name: str, page: int) -> dict:
    client.validate_selection(full_name, "main")
    token = storage.decrypt_token(await storage.get_connection())
    return await client.list_branches(token, full_name, page)


async def resolve_repository_selection(full_name: str, branch: str) -> RepositorySelection:
    client.validate_selection(full_name, branch)
    token = storage.decrypt_token(await storage.get_connection())
    return await client.resolve_selection(token, full_name, branch)
