"""Bounded, read-only GitHub REST calls. Never follow credential-bearing redirects."""

import re
from typing import Any
from urllib.parse import quote

import httpx

from app.github.errors import GitHubError
from app.github.models import RepositorySelection

API_URL = "https://api.github.com"
PAGE_SIZE = 50
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")


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


async def _get(token: str, path: str, params: dict[str, Any] | None = None) -> tuple[Any, bool]:
    try:
        async with httpx.AsyncClient(
            base_url=API_URL, timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        ) as client:
            response = await client.get(path, params=params)
    except httpx.RequestError:
        raise GitHubError(503, "Could not reach GitHub. Try again later.") from None
    if response.status_code == 401:
        raise GitHubError(401, "The GitHub token is invalid or expired. Replace it in GitHub settings.")
    if response.status_code == 429 or (
        response.status_code == 403 and (
            response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers
        )
    ):
        retry = response.headers.get("retry-after", "60")
        seconds = min(int(retry), 86400) if retry.isdigit() else 60
        raise GitHubError(429, f"GitHub rate limit reached. Try again in {seconds} seconds.", seconds)
    if response.status_code == 403:
        raise GitHubError(403, "GitHub denied access. Check repository permissions and organization approval/SSO.")
    if response.status_code == 404:
        raise GitHubError(404, "The repository or branch is unavailable to this token.")
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
        not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] <= 0
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


async def get_user(token: str) -> dict[str, Any]:
    data, _ = await _get(token, "/user")
    if (not isinstance(data, dict) or type(data.get("id")) is not int or data["id"] <= 0
            or not isinstance(data.get("login"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", data["login"])):
        raise _invalid_response()
    return {"id": data["id"], "login": data["login"]}


async def list_repositories(token: str, page: int) -> dict[str, Any]:
    data, more = await _get(token, "/user/repos", {
        "page": page, "per_page": PAGE_SIZE, "sort": "full_name", "direction": "asc",
        "affiliation": "owner,collaborator,organization_member",
    })
    if not isinstance(data, list) or len(data) > PAGE_SIZE:
        raise _invalid_response()
    return {"repositories": [_repository(item) for item in data], "next_page": page + 1 if more else None}


def _repo_path(full_name: str) -> str:
    return "/repos/" + "/".join(quote(part, safe="") for part in full_name.split("/"))


async def list_branches(token: str, full_name: str, page: int) -> dict[str, Any]:
    validate_selection(full_name, "main")
    data, more = await _get(token, _repo_path(full_name) + "/branches", {
        "page": page, "per_page": PAGE_SIZE,
    })
    if (not isinstance(data, list) or len(data) > PAGE_SIZE
            or not all(isinstance(item, dict) and isinstance(item.get("name"), str)
                       and item["name"] for item in data)):
        raise _invalid_response()
    return {"branches": [item["name"] for item in data], "next_page": page + 1 if more else None}


async def resolve_selection(token: str, full_name: str, branch: str) -> RepositorySelection:
    validate_selection(full_name, branch)
    data, _ = await _get(token, _repo_path(full_name))
    repository = _repository(data)
    data, _ = await _get(token, _repo_path(repository["full_name"]) + "/branches/" + quote(branch, safe=""))
    if not isinstance(data, dict) or data.get("name") != branch:
        raise _invalid_response()
    return {"provider": "github", "repository_id": repository["repository_id"],
            "full_name": repository["full_name"], "html_url": repository["html_url"],
            "private": repository["private"], "branch": branch}
