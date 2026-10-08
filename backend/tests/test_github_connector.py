"""Public connector/creation contracts against controlled GitHub HTTP responses."""

from datetime import datetime, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import github, llm_requests, requirements
from app.github import storage

TOKEN = "github_pat_SECRET_SENTINEL"
REPO = {"id": 42, "full_name": "octocat/private-project", "private": True, "default_branch": "main"}


@pytest.fixture
def http(monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setattr(storage, "GITHUB_TOKEN_ENCRYPTION_KEY", key.decode())
    row = {"encrypted_token": Fernet(key).encrypt(TOKEN.encode()), "user_id": 7,
           "login": "octocat", "validated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}

    async def get_connection():
        return row

    monkeypatch.setattr(storage, "get_connection", get_connection)
    app = FastAPI()
    app.include_router(github.router)
    app.include_router(llm_requests.router)
    app.include_router(requirements.router)
    return TestClient(app)


def test_status_is_stored_metadata_not_a_credential_or_live_check(http, github_http, caplog):
    response = http.get("/connectors/github/connection")
    assert response.status_code == 200
    assert response.json() == {"configured": True, "encryption_ready": True,
                               "account": {"id": 7, "login": "octocat"},
                               "validated_at": "2026-01-01T00:00:00+00:00"}
    assert "encrypted_token" not in response.text
    assert TOKEN not in response.text + caplog.text
    assert not github_http[1]


def test_repository_and_branch_pagination_never_follows_foreign_links(http, github_http):
    responses, requests = github_http
    responses["/user/repos"] = lambda request: httpx.Response(
        200, json=[REPO] if request.url.params["page"] == "1" else [],
        headers={"Link": '<https://attacker.invalid/leak>; rel="next"'} if request.url.params["page"] == "1" else {},
    )
    first = http.get("/connectors/github/repositories").json()
    assert first == {"repositories": [{"repository_id": 42, "full_name": "octocat/private-project",
                                      "html_url": "https://github.com/octocat/private-project",
                                      "private": True, "default_branch": "main"}], "next_page": 2}
    assert http.get("/connectors/github/repositories?page=2").json() == {"repositories": [], "next_page": None}
    responses["/repos/octocat/private-project/branches"] = httpx.Response(
        200, json=[{"name": "feature/one"}], headers={"Link": '<https://attacker.invalid>; rel="next"'},
    )
    branches = http.get("/connectors/github/repositories/octocat/private-project/branches").json()
    assert branches == {"branches": ["feature/one"], "next_page": 2}
    assert all(request.url.host == "api.github.com" for request in requests)
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in requests)
    assert all(request.url.params["per_page"] == "50" for request in requests)
    assert http.get("/connectors/github/repositories?page=0").status_code == 422


@pytest.mark.parametrize("upstream,expected", [
    (httpx.Response(401, text=TOKEN), 401),
    (httpx.Response(403, headers={"x-github-sso": "required"}, text=TOKEN), 403),
    (httpx.Response(404, text=TOKEN), 404),
    (httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, text=TOKEN), 429),
    (httpx.Response(429, headers={"retry-after": "30"}, text=TOKEN), 429),
    (httpx.Response(500, text=TOKEN), 502),
    (httpx.Response(302, headers={"location": "https://attacker.invalid"}, text=TOKEN), 502),
    (httpx.Response(200, text=TOKEN), 502),
    (httpx.Response(200, json=[{"id": TOKEN}]), 502),
    (httpx.ReadTimeout(TOKEN), 503),
])
def test_upstream_failures_are_actionable_and_do_not_echo_credentials(http, github_http, caplog, upstream, expected):
    github_http[0]["/user/repos"] = upstream
    response = http.get("/connectors/github/repositories")
    assert response.status_code == expected
    assert isinstance(response.json()["detail"], str)
    assert TOKEN not in response.text + caplog.text
    assert len(github_http[1]) == 1
    if expected == 429:
        assert int(response.headers["retry-after"]) > 0


@pytest.mark.parametrize("method,path,headers,body,expected", [
    ("PUT", "/connection", {"Origin": "https://attacker.invalid"}, {"token": TOKEN}, 403),
    ("POST", "/connection/check", {"Origin": "null"}, {}, 403),
    ("DELETE", "/connection", {"Origin": "https://attacker.invalid"}, {}, 403),
    ("PUT", "/connection", {}, {"token": TOKEN + " "}, 422),
    ("PUT", "/connection", {}, {"token": [TOKEN]}, 422),
    ("PUT", "/connection", {}, {"token": TOKEN, "extra": TOKEN}, 422),
])
def test_connector_mutations_reject_unsafe_origins_and_redact_bad_input(http, github_http, caplog, method, path, headers, body, expected):
    response = http.request(method, "/connectors/github" + path, headers=headers, json=body)
    assert response.status_code == expected
    assert TOKEN not in response.text + caplog.text
    assert not github_http[1]


def test_bad_media_json_and_key_configuration_are_safe(http, github_http, monkeypatch):
    assert http.put("/connectors/github/connection", data={"token": TOKEN}).status_code == 415
    response = http.put("/connectors/github/connection", content='{"token":"' + TOKEN,
                        headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and TOKEN not in response.text
    monkeypatch.setattr(storage, "GITHUB_TOKEN_ENCRYPTION_KEY", "")
    response = http.put("/connectors/github/connection", json={"token": TOKEN}, headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 503 and "GITHUB_TOKEN_ENCRYPTION_KEY" in response.text
    assert not github_http[1]


def test_disconnected_connection_does_not_list_repositories(http, github_http, monkeypatch):
    async def missing():
        return None

    monkeypatch.setattr(storage, "get_connection", missing)
    assert http.get("/connectors/github/connection").json()["configured"] is False
    assert http.get("/connectors/github/repositories").status_code == 409
    assert not github_http[1]


@pytest.mark.parametrize("multipart", [False, True])
def test_standalone_chat_resolves_canonical_selection_for_both_body_formats(http, github_http, monkeypatch, multipart):
    responses, requests = github_http
    responses["/repos/octocat/private-project"] = httpx.Response(200, json=REPO)
    responses["/repos/octocat/private-project/branches/feature/one"] = httpx.Response(200, json={"name": "feature/one"})
    submitted = []

    async def start(message, parent, attachment, fork, repository):
        submitted.append(repository)
        return "llm-request-" + "0" * 32

    monkeypatch.setattr(llm_requests, "start_llm_request", start)
    choice = {"full_name": "octocat/private-project", "branch": "feature/one"}
    if multipart:
        import json
        response = http.post("/llm/requests", files={"message": (None, "Hello"), "repository": (None, json.dumps(choice))})
    else:
        response = http.post("/llm/requests", json={"message": "Hello", "repository": choice})
    assert response.status_code == 202
    assert submitted == [{"provider": "github", "repository_id": 42, "full_name": "octocat/private-project",
                          "html_url": "https://github.com/octocat/private-project", "private": True, "branch": "feature/one"}]
    assert requests[-1].url.raw_path.endswith(b"/branches/feature%2Fone")
    assert TOKEN not in str(submitted) + response.text


def test_invalid_selection_is_rejected_before_upload_storage(http, github_http, monkeypatch):
    import json
    github_http[0]["/repos/octocat/private-project"] = httpx.Response(404)

    async def forbidden_upload(upload):
        pytest.fail("An unavailable repository must be rejected before upload persistence")

    monkeypatch.setattr(llm_requests, "store_original", forbidden_upload)
    response = http.post("/llm/requests", data={"message": "Hello", "repository": json.dumps({
        "full_name": "octocat/private-project", "branch": "main",
    })}, files={"file": ("input.txt", b"Uploaded text", "text/plain")})
    assert response.status_code == 404


@pytest.mark.parametrize("choice", [
    {"full_name": "octocat/../secret", "branch": "main"},
    {"full_name": "https://attacker.invalid/repo", "branch": "main"},
    {"full_name": "octocat/private-project", "branch": "../bad"},
    {"full_name": "octocat/private-project", "branch": "bad\nbranch"},
])
def test_hostile_session_selection_never_sends_authenticated_http(http, github_http, choice):
    response = http.post("/requirements/sessions", json={"repository": choice})
    assert response.status_code == 422
    assert not github_http[1]


@pytest.mark.parametrize("choice", [None, {"full_name": "octocat/private-project", "branch": "main"}])
def test_parented_chat_cannot_override_or_clear_the_repository(http, github_http, choice):
    response = http.post("/llm/requests", json={"message": "Reply", "parent_request_id": "llm-request-" + "0" * 32,
                                               "repository": choice})
    assert response.status_code == 422
    assert not github_http[1]


@pytest.mark.parametrize("body", [None, {}, {"repository": None}])
def test_requirements_creation_requires_a_repository(http, monkeypatch, body):
    async def start(*, repository=None):
        pytest.fail("A session must not start without a repository")

    monkeypatch.setattr(requirements, "start_session", start)
    assert http.post("/requirements/sessions", json=body).status_code == 422
