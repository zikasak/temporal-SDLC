"""Bounded GitHub REST calls as a GitHub App. Never follow credential-bearing redirects."""

import re
import time
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from app.github.errors import GitHubError
from app.github.models import RepositorySelection

API_URL = "https://api.github.com"
PAGE_SIZE = 50
LOGIN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
SLUG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}")


def validate_selection(full_name: str, branch: str) -> None:
    if not NAME_PATTERN.fullmatch(full_name) or full_name.split("/")[-1] in {".", ".."}:
        raise GitHubError(422, "Choose a valid GitHub owner/repository.")
    if (
        not branch or len(branch) > 255 or branch == "@" or "@{" in branch
        or ".." in branch or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", branch)
        or any(not part or part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
        or branch.endswith(".")
    ):
        raise GitHubError(422, "Choose a valid branch name.")


def _invalid_response() -> GitHubError:
    return GitHubError(502, "GitHub returned an invalid response. Try again later.")


def app_token(app_id: int, private_key: str) -> str:
    """A short-lived JWT for the app itself; it reads the app's installations and mints
    their tokens, nothing else."""
    now = int(time.time())
    try:
        # Backdated to tolerate clock drift between this host and GitHub.
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app_id)},
                          private_key, algorithm="RS256")
    except Exception:
        # The cause can quote key material.
        raise GitHubError(422, "Provide the GitHub App's private key as an RSA key in PEM format.") from None


async def _request(method: str, token: str | None, path: str,
                   params: dict[str, Any] | None = None) -> tuple[Any, bool]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(
            base_url=API_URL, timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
            headers=headers,
        ) as client:
            response = await client.request(method, path, params=params)
    except httpx.RequestError:
        raise GitHubError(503, "Could not reach GitHub. Try again later.") from None
    if response.status_code == 401:
        raise GitHubError(401, "GitHub rejected the app credentials. Replace the app ID and private key in GitHub settings.")
    if response.status_code == 429 or (
        response.status_code == 403 and (
            response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers
        )
    ):
        retry = response.headers.get("retry-after", "60")
        seconds = min(int(retry), 86400) if retry.isdigit() else 60
        raise GitHubError(429, f"GitHub rate limit reached. Try again in {seconds} seconds.", seconds)
    if response.status_code == 403:
        raise GitHubError(403, "GitHub denied access. Check the GitHub App's permissions and the repositories it is installed on.")
    if response.status_code == 404:
        raise GitHubError(404, "The repository or branch is unavailable to the GitHub App installation.")
    if not 200 <= response.status_code < 300:
        raise GitHubError(502, "GitHub could not complete the request. Refresh the repository list and try again.")
    try:
        data = response.json()
    except ValueError:
        raise _invalid_response() from None
    # The presence of a next relation is enough: construct page+1 ourselves, never
    # follow an upstream Link URL, even if it points at another host.
    return data, "next" in response.links


def _repository(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict) or not _positive(value.get("id"))
        or not isinstance(value.get("full_name"), str)
        or not NAME_PATTERN.fullmatch(value["full_name"])
        or value["full_name"].split("/")[-1] in {".", ".."}
        or type(value.get("private")) is not bool
        or not isinstance(value.get("default_branch"), str)
    ):
        raise _invalid_response()
    return {"repository_id": value["id"], "full_name": value["full_name"],
            "html_url": f"https://github.com/{value['full_name']}",
            "private": value["private"], "default_branch": value["default_branch"]}


def _positive(value: Any) -> bool:
    return type(value) is int and value > 0


def _installation(value: Any) -> dict[str, Any]:
    account = value.get("account") if isinstance(value, dict) else None
    if (not isinstance(account, dict) or not _positive(value.get("id"))
            or not isinstance(account.get("login"), str)
            or not LOGIN_PATTERN.fullmatch(account["login"])):
        raise _invalid_response()
    return {"id": value["id"], "account": account["login"]}


def _removed(exc: GitHubError) -> GitHubError:
    if exc.status_code != 404:
        return exc
    return GitHubError(409, "The GitHub App is no longer installed there. Connect it again in GitHub settings.")


async def convert_manifest(code: str) -> dict[str, Any]:
    """Exchange the one-time code GitHub hands back after creating an app from a manifest
    for the app's identity and private key. GitHub accepts a code once, for an hour."""
    try:
        data, _ = await _request(
            "POST", None, f"/app-manifests/{quote(code, safe='')}/conversions")
    except GitHubError as exc:
        if exc.status_code not in (404, 422):
            raise
        raise GitHubError(409, "GitHub no longer accepts this app creation code. Create the app again.") from None
    if (not isinstance(data, dict) or not _positive(data.get("id"))
            or not isinstance(data.get("slug"), str) or not SLUG_PATTERN.fullmatch(data["slug"])
            or not isinstance(data.get("pem"), str) or not 1 <= len(data["pem"]) <= 8192):
        raise _invalid_response()
    return {"id": data["id"], "slug": data["slug"], "private_key": data["pem"]}


async def get_app(app_token: str) -> dict[str, Any]:
    data, _ = await _request("GET", app_token, "/app")
    if (not isinstance(data, dict) or not _positive(data.get("id"))
            or not isinstance(data.get("slug"), str) or not SLUG_PATTERN.fullmatch(data["slug"])):
        raise _invalid_response()
    return {"id": data["id"], "slug": data["slug"]}


async def list_installations(app_token: str) -> list[dict[str, Any]]:
    """The accounts the app is installed on (the first hundred)."""
    data, _ = await _request("GET", app_token, "/app/installations", {"per_page": 100})
    if not isinstance(data, list) or len(data) > 100:
        raise _invalid_response()
    return [_installation(item) for item in data]


async def get_installation(app_token: str, installation_id: int) -> dict[str, Any]:
    try:
        data, _ = await _request("GET", app_token, f"/app/installations/{installation_id}")
    except GitHubError as exc:
        raise _removed(exc) from None
    return _installation(data)


async def installation_token(app_token: str, installation_id: int) -> str:
    """A token acting as the installation. GitHub expires it after an hour: mint one per
    operation and never store it."""
    try:
        data, _ = await _request(
            "POST", app_token, f"/app/installations/{installation_id}/access_tokens")
    except GitHubError as exc:
        raise _removed(exc) from None
    token = data.get("token") if isinstance(data, dict) else None
    if (not isinstance(token, str) or not 1 <= len(token) <= 512
            or any(not 33 <= ord(char) <= 126 for char in token)):
        raise _invalid_response()
    return token


async def get_bot_user_id(token: str, slug: str) -> int:
    """The ID of the app's bot user, which attributes the commits it authors."""
    data, _ = await _request("GET", token, "/users/" + quote(f"{slug}[bot]", safe=""))
    if not isinstance(data, dict) or not _positive(data.get("id")):
        raise _invalid_response()
    return data["id"]


async def list_repositories(token: str, page: int) -> dict[str, Any]:
    data, more = await _request("GET", token, "/installation/repositories", {
        "page": page, "per_page": PAGE_SIZE,
    })
    items = data.get("repositories") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) > PAGE_SIZE:
        raise _invalid_response()
    return {"repositories": [_repository(item) for item in items], "next_page": page + 1 if more else None}


def _repo_path(full_name: str) -> str:
    return "/repos/" + "/".join(quote(part, safe="") for part in full_name.split("/"))


async def list_branches(token: str, full_name: str, page: int) -> dict[str, Any]:
    validate_selection(full_name, "main")
    data, more = await _request("GET", token, _repo_path(full_name) + "/branches", {
        "page": page, "per_page": PAGE_SIZE,
    })
    if (not isinstance(data, list) or len(data) > PAGE_SIZE
            or not all(isinstance(item, dict) and isinstance(item.get("name"), str)
                       and item["name"] for item in data)):
        raise _invalid_response()
    return {"branches": [item["name"] for item in data], "next_page": page + 1 if more else None}


async def resolve_selection(token: str, full_name: str, branch: str) -> RepositorySelection:
    validate_selection(full_name, branch)
    data, _ = await _request("GET", token, _repo_path(full_name))
    repository = _repository(data)
    data, _ = await _request("GET", token, _repo_path(repository["full_name"]) + "/branches/" + quote(branch, safe=""))
    if not isinstance(data, dict) or data.get("name") != branch:
        raise _invalid_response()
    return {"provider": "github", "repository_id": repository["repository_id"],
            "full_name": repository["full_name"], "html_url": repository["html_url"],
            "private": repository["private"], "branch": branch}
