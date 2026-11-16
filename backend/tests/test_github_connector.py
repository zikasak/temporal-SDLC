"""Public connector/creation contracts against controlled GitHub HTTP responses."""

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import github, llm_requests, requirements
from app.github import storage

TOKEN = "ghs_SECRET_SENTINEL"
TOKEN_PATH = "/app/installations/99/access_tokens"
# Never parsed: these submissions are rejected first.
PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nKEY_SENTINEL\n-----END RSA PRIVATE KEY-----\n"
CREDENTIALS = {"app_id": 123, "private_key": PRIVATE_KEY}
REPO = {"id": 42, "full_name": "octocat/private-project", "private": True, "default_branch": "main"}


@pytest.fixture
def http(github_connection):
    github_connection(TOKEN)
    app = FastAPI()
    app.include_router(github.router)
    app.include_router(llm_requests.router)
    app.include_router(requirements.router)
    return TestClient(app)


def test_status_is_stored_metadata_not_a_credential_or_live_check(http, github_http, caplog):
    response = http.get("/connectors/github/connection")
    assert response.status_code == 200
    assert response.json() == {"configured": True, "encryption_ready": True,
                               "app": {"id": 123, "slug": "my-app"},
                               "installation": {"id": 99, "account": "octocat"},
                               "validated_at": "2026-01-01T00:00:00+00:00",
                               "webhook_secret_set": False, "webhook_events_missing": False}
    assert "encrypted_private_key" not in response.text
    assert TOKEN not in response.text + caplog.text
    assert not github_http[1]


def test_repository_and_branch_pagination_never_follows_foreign_links(http, github_http, app_private_key):
    responses, requests = github_http
    responses["/installation/repositories"] = lambda request: httpx.Response(
        200, json={"total_count": 1, "repositories": [REPO] if request.url.params["page"] == "1" else []},
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
    # Each operation mints its own installation token, as the app, and acts with it.
    minted = [request for request in requests if request.url.path == TOKEN_PATH]
    used = [request for request in requests if request.url.path != TOKEN_PATH]
    public_key = load_pem_private_key(app_private_key.encode(), None).public_key()
    assert len(minted) == len(used) == 3
    for request in minted:
        claims = jwt.decode(request.headers["authorization"].removeprefix("Bearer "), public_key,
                            algorithms=["RS256"], issuer="123")
        assert request.method == "POST" and claims["exp"] - claims["iat"] <= 600
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in used)
    assert all(request.url.params["per_page"] == "50" for request in used)
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
    (httpx.Response(200, json={"repositories": [{"id": TOKEN}]}), 502),
    (httpx.ReadTimeout(TOKEN), 503),
])
def test_upstream_failures_are_actionable_and_do_not_echo_credentials(http, github_http, caplog, upstream, expected):
    github_http[0]["/installation/repositories"] = upstream
    response = http.get("/connectors/github/repositories")
    assert response.status_code == expected
    assert isinstance(response.json()["detail"], str)
    assert TOKEN not in response.text + caplog.text
    assert len(github_http[1]) == 2
    if expected == 429:
        assert int(response.headers["retry-after"]) > 0


def test_an_installation_token_of_any_length_is_used_as_given(http, github_http):
    token = "ghs_" + "a1B2_" * 200
    github_http[0][TOKEN_PATH] = httpx.Response(201, json={"token": token})
    github_http[0]["/installation/repositories"] = httpx.Response(200, json={"repositories": [REPO]})
    assert http.get("/connectors/github/repositories").status_code == 200
    assert github_http[1][-1].headers["authorization"] == f"Bearer {token}"


@pytest.mark.parametrize("upstream,expected", [
    (httpx.Response(401), 401),
    (httpx.Response(404), 409),  # The installation was removed.
    (httpx.Response(201, json={"token": "not a\ntoken"}), 502),
    (httpx.ConnectError(TOKEN), 503),
])
def test_repositories_are_not_requested_without_an_installation_token(http, github_http, upstream, expected):
    github_http[0][TOKEN_PATH] = upstream
    response = http.get("/connectors/github/repositories")
    assert response.status_code == expected
    assert [request.url.path for request in github_http[1]] == [TOKEN_PATH]


@pytest.mark.parametrize("method,path,headers,body,expected", [
    ("PUT", "/connection", {"Origin": "https://attacker.invalid"}, CREDENTIALS, 403),
    ("POST", "/connection/check", {"Origin": "null"}, {}, 403),
    ("POST", "/connection/manifest", {"Origin": "https://attacker.invalid"}, {"code": "v1.code"}, 403),
    ("POST", "/connection/manifest", {}, {"code": "../../app"}, 422),
    ("POST", "/connection/manifest", {}, {"code": "v1.code", "extra": 1}, 422),
    ("DELETE", "/connection", {"Origin": "https://attacker.invalid"}, {}, 403),
    ("PUT", "/connection", {}, {"private_key": PRIVATE_KEY}, 422),
    ("PUT", "/connection", {}, {**CREDENTIALS, "app_id": "123"}, 422),
    ("PUT", "/connection", {}, {**CREDENTIALS, "private_key": [PRIVATE_KEY]}, 422),
    ("PUT", "/connection", {}, {**CREDENTIALS, "private_key": PRIVATE_KEY + "\x00"}, 422),
    ("PUT", "/connection", {}, {**CREDENTIALS, "account": "octocat/../x"}, 422),
    ("PUT", "/connection", {}, {**CREDENTIALS, "extra": PRIVATE_KEY}, 422),
    # Well-formed input whose key is not one.
    ("PUT", "/connection", {}, CREDENTIALS, 422),
])
def test_connector_mutations_reject_unsafe_origins_and_redact_bad_input(http, github_http, caplog, method, path, headers, body, expected):
    response = http.request(method, "/connectors/github" + path, headers=headers, json=body)
    assert response.status_code == expected
    assert "KEY_SENTINEL" not in response.text + caplog.text
    assert not github_http[1]


def test_bad_media_json_and_key_configuration_are_safe(http, github_http, monkeypatch):
    assert http.put("/connectors/github/connection", data={"private_key": PRIVATE_KEY}).status_code == 415
    response = http.put("/connectors/github/connection", content='{"private_key":"KEY_SENTINEL',
                        headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and "KEY_SENTINEL" not in response.text
    monkeypatch.setattr(storage, "GITHUB_TOKEN_ENCRYPTION_KEY", "")
    response = http.put("/connectors/github/connection", json=CREDENTIALS, headers={"Origin": "http://localhost:5173"})
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

    monkeypatch.setattr(requirements, "start_task", start)
    assert http.post("/requirements/sessions", json=body).status_code == 422


INTAKE = {"kind": "api_change", "answers": ["One existing endpoint", "Add new behaviour", "Yes"],
          "task": "Add paging to the room list"}


@pytest.mark.parametrize("change", [
    {"kind": "maintenance"},
    {"answers": ["One existing endpoint", "Add new behaviour"]},
    {"answers": ["One existing endpoint", "  ", "Yes"]},
    {"task": "   "},
])
def test_requirements_creation_requires_the_intake(http, monkeypatch, change):
    async def start(**kwargs):
        pytest.fail("A session must not start without the intake")

    monkeypatch.setattr(requirements, "start_task", start)
    body = {"repository": {"full_name": "octocat/private-project", "branch": "main"},
            **INTAKE, **change}
    assert http.post("/requirements/sessions", json=body).status_code == 422


def test_intake_lists_the_questions_of_every_kind(http):
    from app.llm.questions import normalize_questions

    intake = http.get("/requirements/intake").json()
    assert [item["kind"] for item in intake["kinds"]] == list(intake["questions"])
    for questions in intake["questions"].values():
        assert normalize_questions(questions) == questions
