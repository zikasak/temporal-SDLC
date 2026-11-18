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
                   params: dict[str, Any] | None = None,
                   json: dict[str, Any] | None = None) -> tuple[Any, bool]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(
            base_url=API_URL, timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
            headers=headers,
        ) as client:
            response = await client.request(method, path, params=params, json=json)
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
    if response.status_code == 422 and json is not None:
        # Callers that send a body can act on its rejection, apart from other failures.
        raise GitHubError(422, "GitHub could not process the request.")
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
    return {"id": value["id"], "account": account["login"],
            "permissions": _permissions(value.get("permissions"))}


def _permissions(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items()
            if isinstance(key, str) and isinstance(item, str)}


# The events and the permission mentions of the app need.
MENTION_EVENTS = frozenset({"issue_comment", "pull_request_review_comment"})


def events_missing(app: dict[str, Any], installation: dict[str, Any] | None) -> bool:
    """Whether the app misses an event mentions arrive by, or the permission to reply:
    the installation's when it is installed, else the app's own."""
    permissions = installation["permissions"] if installation else app.get("permissions", {})
    return (not MENTION_EVENTS <= set(app.get("events") or ())
            or permissions.get("pull_requests") != "write")


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
    secret = data.get("webhook_secret")
    return {"id": data["id"], "slug": data["slug"], "private_key": data["pem"],
            "webhook_secret": secret if isinstance(secret, str) and secret else None,
            "events": [item for item in data.get("events") or [] if isinstance(item, str)],
            "permissions": _permissions(data.get("permissions"))}


async def get_app(app_token: str) -> dict[str, Any]:
    data, _ = await _request("GET", app_token, "/app")
    if (not isinstance(data, dict) or not _positive(data.get("id"))
            or not isinstance(data.get("slug"), str) or not SLUG_PATTERN.fullmatch(data["slug"])):
        raise _invalid_response()
    events = data.get("events")
    external_url = data.get("external_url")
    return {"id": data["id"], "slug": data["slug"],
            "external_url": external_url if isinstance(external_url, str) else None,
            "events": [item for item in events if isinstance(item, str)]
            if isinstance(events, list) else [],
            "permissions": _permissions(data.get("permissions"))}


async def get_hook_config(app_token: str) -> dict[str, Any]:
    """The app's webhook configuration: its URL, without the secret GitHub masks."""
    data, _ = await _request("GET", app_token, "/app/hook/config")
    if not isinstance(data, dict):
        raise _invalid_response()
    url = data.get("url")
    return {"url": url if isinstance(url, str) else None}


async def update_hook_config(app_token: str, url: str, secret: str) -> None:
    """Point the app's webhook at `url`, signed with `secret`."""
    await _request("PATCH", app_token, "/app/hook/config",
                   json={"url": url, "content_type": "json", "secret": secret,
                         "insecure_ssl": "0"})


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
    # Opaque, of no fixed length; it only has to be safe in a header.
    if (not isinstance(token, str) or not token
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


# The pull request permission the installation needs; reported when it is missing.
PULL_REQUEST_PERMISSION = (
    "The GitHub App installation lacks the pull_requests: write permission, so the pull "
    "request could not be opened or updated. Grant the permission to the app, accept it "
    "for the installation, then retry the repository stage.")
TITLE_CHARS = 256
BODY_CHARS = 65000


def _pull_request_denied(exc: GitHubError) -> GitHubError:
    return GitHubError(403, PULL_REQUEST_PERMISSION) if exc.status_code == 403 else exc


def _pull_request(value: Any, full_name: str) -> dict[str, Any]:
    base = value.get("base") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or not _positive(value.get("number"))
            or not isinstance(value.get("title"), str)
            or type(value.get("draft")) is not bool
            or not isinstance(value.get("node_id"), str) or not value["node_id"]
            or not isinstance(base, dict) or not isinstance(base.get("ref"), str)):
        raise _invalid_response()
    # Built from validated parts, never taken from the response as a link to follow.
    return {"number": value["number"], "title": value["title"][:TITLE_CHARS],
            "url": f"https://github.com/{full_name}/pull/{value['number']}",
            "draft": value["draft"], "base": base["ref"], "node_id": value["node_id"]}


async def get_default_branch(token: str, full_name: str) -> str:
    validate_selection(full_name, "main")
    data, _ = await _request("GET", token, _repo_path(full_name))
    return _repository(data)["default_branch"]


async def find_open_pull_request(token: str, full_name: str, branch: str,
                                 base: str) -> dict[str, Any] | None:
    """The open pull request from `branch` of the repository itself to `base`, if any."""
    validate_selection(full_name, branch)
    owner = full_name.split("/")[0]
    try:
        data, _ = await _request("GET", token, _repo_path(full_name) + "/pulls", {
            "head": f"{owner}:{branch}", "base": base, "state": "open", "per_page": 10})
    except GitHubError as exc:
        raise _pull_request_denied(exc) from None
    if not isinstance(data, list):
        raise _invalid_response()
    items = [_pull_request(item, full_name) for item in data]
    return items[0] if items else None


async def create_pull_request(token: str, full_name: str, branch: str, base: str,
                              title: str, body: str, draft: bool) -> dict[str, Any]:
    validate_selection(full_name, branch)
    payload = {"title": title[:TITLE_CHARS], "head": branch, "base": base,
               "body": body[:BODY_CHARS], "draft": draft}
    try:
        data, _ = await _request("POST", token, _repo_path(full_name) + "/pulls",
                                 json=payload)
    except GitHubError as exc:
        if exc.status_code != 422 or not draft:
            raise _pull_request_denied(exc) from None
        # Repositories on some plans support no draft pull requests: open a ready one,
        # whose description still says that the delivery is unverified.
        try:
            data, _ = await _request("POST", token, _repo_path(full_name) + "/pulls",
                                     json={**payload, "draft": False})
        except GitHubError as retried:
            raise _pull_request_denied(retried) from None
    return _pull_request(data, full_name)


async def update_pull_request(token: str, full_name: str, number: int, title: str,
                              body: str) -> dict[str, Any]:
    validate_selection(full_name, "main")
    try:
        data, _ = await _request(
            "PATCH", token, f"{_repo_path(full_name)}/pulls/{number}",
            json={"title": title[:TITLE_CHARS], "body": body[:BODY_CHARS]})
    except GitHubError as exc:
        raise _pull_request_denied(exc) from None
    return _pull_request(data, full_name)


async def set_pull_request_draft(token: str, node_id: str, draft: bool) -> bool:
    """Mark a pull request ready for review, or convert it back to a draft; the REST API
    has no call for either. Returns the pull request's draft flag afterwards."""
    mutation = "convertPullRequestToDraft" if draft else "markPullRequestReadyForReview"
    query = (f"mutation($id: ID!) {{ {mutation}(input: {{pullRequestId: $id}}) "
             "{ pullRequest { isDraft } } }")
    try:
        data, _ = await _request("POST", token, "/graphql",
                                 json={"query": query, "variables": {"id": node_id}})
    except GitHubError as exc:
        raise _pull_request_denied(exc) from None
    if not isinstance(data, dict):
        raise _invalid_response()
    errors = data.get("errors")
    if errors:
        if isinstance(errors, list) and any(
                isinstance(item, dict) and item.get("type") == "FORBIDDEN" for item in errors):
            raise GitHubError(403, PULL_REQUEST_PERMISSION)
        raise GitHubError(502, "GitHub could not change the pull request's draft state.")
    payload = data.get("data")
    result = payload.get(mutation) if isinstance(payload, dict) else None
    pull = result.get("pullRequest") if isinstance(result, dict) else None
    if not isinstance(pull, dict) or type(pull.get("isDraft")) is not bool:
        raise _invalid_response()
    return pull["isDraft"]


async def get_pull_request_state(token: str, full_name: str,
                                 number: int) -> dict[str, Any] | None:
    """The pull request's current state, merged flag and author; None when it is gone."""
    validate_selection(full_name, "main")
    try:
        data, _ = await _request("GET", token, f"{_repo_path(full_name)}/pulls/{number}")
    except GitHubError as exc:
        if exc.status_code == 404:
            return None
        raise
    if not isinstance(data, dict) or not isinstance(data.get("state"), str):
        raise _invalid_response()
    user = data.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    return {"state": data["state"], "merged": data.get("merged") is True,
            "author": author if isinstance(author, str) else None}


WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})
COMMENT_CHARS = 65000


async def has_write_access(token: str, full_name: str, login: str) -> bool:
    """Whether the GitHub user `login` may write to the repository."""
    validate_selection(full_name, "main")
    if not LOGIN_PATTERN.fullmatch(login):
        return False
    try:
        data, _ = await _request(
            "GET", token,
            f"{_repo_path(full_name)}/collaborators/{quote(login, safe='')}/permission")
    except GitHubError as exc:
        # Not a collaborator at all.
        if exc.status_code == 404:
            return False
        raise
    if not isinstance(data, dict):
        raise _invalid_response()
    role = data.get("role_name")
    return data.get("permission") in WRITE_PERMISSIONS or role in WRITE_PERMISSIONS


def _comment_url(data: Any, full_name: str, number: int) -> str:
    if not isinstance(data, dict) or not _positive(data.get("id")):
        raise _invalid_response()
    url = data.get("html_url")
    # Kept only when it is a link to this repository on GitHub.
    prefix = f"https://github.com/{full_name}/pull/{number}"
    if isinstance(url, str) and url.startswith(prefix):
        return url
    return f"{prefix}#issuecomment-{data['id']}"


async def comment_on_pull_request(token: str, full_name: str, number: int, body: str) -> str:
    """Add a conversation comment to the pull request; returns its link."""
    validate_selection(full_name, "main")
    try:
        data, _ = await _request("POST", token, f"{_repo_path(full_name)}/issues/{number}/comments",
                                 json={"body": body[:COMMENT_CHARS]})
    except GitHubError as exc:
        raise _pull_request_denied(exc) from None
    return _comment_url(data, full_name, number)


async def reply_to_review_comment(token: str, full_name: str, number: int, comment_id: int,
                                  body: str) -> str:
    """Reply in the thread of an inline review comment; returns the reply's link.
    `comment_id` must be the thread's top-level comment: GitHub takes no reply to a reply."""
    validate_selection(full_name, "main")
    try:
        data, _ = await _request(
            "POST", token, f"{_repo_path(full_name)}/pulls/{number}/comments/{comment_id}/replies",
            json={"body": body[:COMMENT_CHARS]})
    except GitHubError as exc:
        raise _pull_request_denied(exc) from None
    return _comment_url(data, full_name, number)


CHECK_RUNS_PAGE = 100
# How many pages of check runs are read at most: a commit with more is judged on these.
CHECK_RUN_PAGES = 10
# Where GitHub redirects the download of an Actions job's log: signed storage URLs, which
# are fetched without the installation token.
LOG_HOST_SUFFIXES = (".actions.githubusercontent.com", ".blob.core.windows.net")
# How much of a job's log is downloaded at most; its end is what is kept.
LOG_DOWNLOAD_BYTES = 20 * 1024 * 1024
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


def _check_run(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not _positive(value.get("id")):
        return None
    name = value.get("name")
    app = value.get("app")
    output = value.get("output")
    if not isinstance(output, dict):
        output = {}

    def text(item: Any, limit: int) -> str | None:
        return item[:limit] if isinstance(item, str) and item else None

    return {
        "id": value["id"],
        "name": name[:200] if isinstance(name, str) and name else f"check {value['id']}",
        "status": text(value.get("status"), 40) or "queued",
        "conclusion": text(value.get("conclusion"), 40),
        "url": text(value.get("html_url"), 500),
        "app": text(app.get("slug") if isinstance(app, dict) else None, 100),
        "summary": text(output.get("summary"), 4000),
        "text": text(output.get("text"), 4000),
    }


async def list_check_runs(token: str, full_name: str,
                          commit: str) -> tuple[list[dict[str, Any]], bool]:
    """The latest check run of each check of `commit`, such as each GitHub Actions job,
    from every page GitHub lists them on, up to CHECK_RUN_PAGES pages; and whether that
    is all of them. Needs the installation's checks: read permission; GitHub answers 403
    without it."""
    validate_selection(full_name, "main")
    if not COMMIT_PATTERN.fullmatch(commit):
        raise GitHubError(422, "Choose a full commit SHA.")
    runs: list[dict[str, Any]] = []
    for page in range(1, CHECK_RUN_PAGES + 1):
        data, more = await _request(
            "GET", token, f"{_repo_path(full_name)}/commits/{commit}/check-runs",
            params={"per_page": CHECK_RUNS_PAGE, "filter": "latest", "page": page})
        if not isinstance(data, dict) or not isinstance(data.get("check_runs"), list):
            raise _invalid_response()
        runs.extend(run for run in map(_check_run, data["check_runs"]) if run is not None)
        if not more:
            return runs, True
    return runs, False


def _log_location(response: httpx.Response) -> str:
    location = response.headers.get("location", "")
    parsed = httpx.URL(location) if location else None
    if (parsed is None or parsed.scheme != "https"
            or not any(parsed.host.endswith(suffix) for suffix in LOG_HOST_SUFFIXES)):
        raise GitHubError(502, "GitHub redirected the job log to an unexpected address.")
    return location


async def job_log_tail(token: str, full_name: str, job_id: int, limit: int) -> str:
    """The last `limit` characters of the log of GitHub Actions job `job_id`. Needs the
    installation's actions: read permission. GitHub answers with a redirect to a signed
    download address, which is fetched without the installation token."""
    validate_selection(full_name, "main")
    if type(job_id) is not int or job_id <= 0:
        raise GitHubError(422, "Choose a valid job.")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(30, connect=5)
    try:
        async with httpx.AsyncClient(base_url=API_URL, timeout=timeout,
                                     follow_redirects=False, headers=headers) as client:
            response = await client.get(f"{_repo_path(full_name)}/actions/jobs/{job_id}/logs")
        if response.status_code in (403, 404, 410):
            raise GitHubError(response.status_code, "GitHub did not give the job's log.")
        if response.status_code not in (301, 302, 303, 307, 308):
            raise GitHubError(502, "GitHub could not give the job's log.")
        location = _log_location(response)
        tail = bytearray()
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream("GET", location) as download:
                if download.status_code != 200:
                    raise GitHubError(502, "The job's log could not be downloaded.")
                read = 0
                async for chunk in download.aiter_bytes():
                    read += len(chunk)
                    tail.extend(chunk)
                    # Twice the characters asked for covers multi-byte text.
                    del tail[:-limit * 2]
                    if read >= LOG_DOWNLOAD_BYTES:
                        break
    except httpx.RequestError:
        raise GitHubError(503, "Could not reach GitHub. Try again later.") from None
    return bytes(tail).decode("utf-8", errors="replace")[-limit:]
