"""Sign-in details, session and chat input bounds, GitHub input validation and
unavailable dependencies."""
import uuid

import pytest

from conftest import PASSWORD, new_client


def requirements_session() -> str:
    return f"/requirements/sessions/requirements-session-{uuid.uuid4().hex}"


def api_design_session() -> str:
    return f"/api-design/sessions/api-design-session-{uuid.uuid4().hex}"


def test_sign_in_ignores_login_case_and_whitespace_and_sets_a_strict_cookie(
        admin_session, base_url):
    with new_client(base_url) as client:
        response = client.post("/auth/login", json={
            "login": f"  {admin_session['login'].upper()}\t", "password": PASSWORD})
        if response.status_code == 401:
            pytest.fail("Sign-in with the suite's administrator password failed; "
                        "is the admin from CONTRACT_TESTS_ADMIN_LOGIN?", pytrace=False)
        assert response.status_code == 200, response.text
        session = response.json()
        assert session["login"] == admin_session["login"]
        assert session["admin"] is True and session["token_type"] == "bearer"
        assert PASSWORD not in response.text
        cookie = response.headers["set-cookie"]
        assert cookie.startswith(f"access_token={session['access_token']}")
        attributes = cookie.lower()
        assert "httponly" in attributes and "samesite=strict" in attributes
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {session['access_token']}"})
        assert me.status_code == 200 and me.json()["admin"] is True


def test_unknown_login_is_rejected_like_a_wrong_password(anonymous):
    response = anonymous.post("/auth/login", json={"login": f"nobody-{uuid.uuid4().hex[:12]}",
                                                  "password": PASSWORD})
    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)
    assert "access_token" not in response.cookies


def test_session_endpoints_require_sign_in(anonymous):
    for method, path, body in (
            ("POST", "/requirements/sessions", {
                "repository": {"full_name": "owner/repo", "branch": "main"},
                "kind": "docs", "answers": [], "task": "Document it."}),
            ("GET", "/workflows", None),
            ("POST", "/llm/requests", {"message": "hello"}),
            ("GET", "/connectors/github/repositories", None)):
        response = anonymous.request(method, path, json=body)
        assert response.status_code == 401, path
        assert response.headers["www-authenticate"] == "Bearer", path


@pytest.mark.parametrize("session", (requirements_session, api_design_session),
                         ids=("requirements", "api-design"))
def test_messages_to_unknown_sessions_are_not_found(admin, session):
    path = session()
    assert admin.post(f"{path}/messages", json={"message": "Hello"}).status_code == 404
    assert admin.post(f"{path}/answers", json={"answers": ["Yes"]}).status_code == 404
    assert admin.post(f"{path}/fork", json={"message_index": 0}).status_code == 404
    assert admin.post(f"{path}/retry").status_code == 404
    assert admin.post(f"{path}/finalize").status_code == 404
    assert admin.get(f"{path}/document").status_code == 404


def test_checkout_of_an_unknown_session_is_not_found(admin):
    assert admin.post(f"{requirements_session()}/checkout").status_code == 404


def test_api_design_needs_an_existing_requirements_session(admin):
    response = admin.post('/api-design/sessions', json={'requirements_session_id': f'requirements-session-{uuid.uuid4().hex}'})
    assert response.status_code == 404
    assert isinstance(response.json()['detail'], str)


def test_api_design_sessions_cannot_be_started_directly(admin):
    # FR-82: an API design is never started by hand; there is no POST /api-design/sessions.
    response = admin.post("/api-design/sessions", json={
        "requirements_session_id": f"requirements-session-{uuid.uuid4().hex}"})
    assert response.status_code in (404, 405), response.text


@pytest.mark.parametrize("session", (requirements_session, api_design_session),
                         ids=("requirements", "api-design"))
@pytest.mark.parametrize("message", ("", "   ", "x" * 10001), ids=("empty", "blank", "too-long"))
def test_messages_are_one_to_10000_characters(admin, session, message):
    response = admin.post(f"{session()}/messages", json={"message": message})
    assert response.status_code in (413, 422)
    assert response.json()["detail"]


@pytest.mark.parametrize("session", (requirements_session, api_design_session),
                         ids=("requirements", "api-design"))
def test_fork_needs_a_message_index(admin, session):
    for body in ({}, {"message_index": "first"}):
        assert admin.post(f"{session()}/fork", json=body).status_code == 422, body


def test_answers_must_be_a_list(admin):
    response = admin.post(f"{requirements_session()}/answers", json={"answers": "yes"})
    assert response.status_code == 422


def test_requirements_task_is_at_most_10000_characters(admin):
    response = admin.post("/requirements/sessions", json={
        "repository": {"full_name": "owner/repo", "branch": "main"},
        "kind": "docs", "answers": [], "task": "x" * 10001})
    assert response.status_code in (413, 422)


def test_requirements_session_kind_must_be_documented(admin):
    response = admin.post("/requirements/sessions", json={
        "repository": {"full_name": "owner/repo", "branch": "main"},
        "kind": "refactoring", "answers": [], "task": "Tidy up."})
    assert response.status_code == 422


def test_chat_message_is_at_most_10000_characters(admin):
    response = admin.post("/llm/requests", json={"message": "x" * 10001})
    assert response.status_code in (413, 422)


def test_chat_request_needs_a_message(admin):
    assert admin.post("/llm/requests", json={}).status_code == 422


def test_chat_actions_on_unknown_requests_are_not_found(admin):
    path = f"/llm/requests/llm-request-{uuid.uuid4().hex}"
    assert admin.post(f"{path}/retry").status_code == 404
    assert admin.post(f"{path}/fork", json={"message_index": 0}).status_code == 404
    assert admin.get(f"{path}/documents/{uuid.uuid4().hex}").status_code == 404


@pytest.mark.parametrize("page", (0, 10001, "two"))
def test_repository_page_is_validated(admin, page):
    assert admin.get("/connectors/github/repositories",
                     params={"page": page}).status_code == 422


def test_branch_page_is_validated(admin):
    response = admin.get("/connectors/github/repositories/owner/repo/branches",
                         params={"page": 0})
    assert response.status_code == 422


def test_github_configuration_input_is_bounded_without_echoing_the_key(admin):
    secret = "k" * (17 * 1024)
    key = f"-----BEGIN PRIVATE KEY-----\n{secret}\n-----END PRIVATE KEY-----"
    response = admin.put("/connectors/github/connection", json={"app_id": 1, "private_key": key})
    assert response.status_code in (413, 422)
    assert secret[:64] not in response.text
    assert admin.get("/connectors/github/connection").json()["configured"] is False


def test_github_configuration_needs_a_private_key(admin):
    response = admin.put("/connectors/github/connection", json={"app_id": 1})
    assert response.status_code == 422


def test_manifest_connection_needs_a_code(admin):
    assert admin.post("/connectors/github/connection/manifest", json={}).status_code == 422
