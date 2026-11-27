"""What the API answers without Temporal, an LLM provider or GitHub behind it."""


def test_github_connection_starts_unconfigured(admin):
    response = admin.get("/connectors/github/connection")
    assert response.status_code == 200
    connection = response.json()
    assert connection["configured"] is False
    assert connection["app"] is None and connection["installation"] is None
    assert "private_key" not in connection


def test_github_configuration_must_be_json(admin):
    response = admin.put("/connectors/github/connection", content="app_id=1",
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 415


def test_github_configuration_is_validated_without_echoing_the_key(admin):
    key = "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----"
    response = admin.put("/connectors/github/connection",
                         json={"app_id": "one", "private_key": key})
    assert response.status_code == 422
    assert "not-a-key" not in response.text


def test_unknown_ids_are_not_found(admin):
    for path in ("/requirements/sessions/requirements-session-unknown",
                 "/api-design/sessions/api-design-session-unknown",
                 "/llm/requests/llm-request-unknown"):
        assert admin.get(path).status_code == 404, path


def test_requirements_session_needs_a_task(admin):
    response = admin.post("/requirements/sessions", json={
        "repository": {"full_name": "owner/repo", "branch": "main"},
        "kind": "docs", "answers": [], "task": "   "})
    assert response.status_code == 422


def test_unknown_workflow_type_is_rejected(admin):
    response = admin.get("/workflows", params={"type": "NoSuchWorkflow"})
    assert response.status_code == 422
