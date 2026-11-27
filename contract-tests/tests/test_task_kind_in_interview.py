"""The kind of task moved into the interview (FR-224 to FR-230): the intake endpoint is gone and
POST /requirements/sessions takes only the repository, its branch and the task. Recording the
kind and the 409 of finalizing without one need a running interview (GitHub, Temporal and an
LLM), so only what the service answers before contacting them is checked here."""
import uuid

import pytest

VALID = {"repository": {"full_name": "owner/repo", "branch": "main"},
         "task": "Document the API."}


def assert_unprocessable(response):
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


def test_intake_endpoint_is_removed(admin):
    # FR-227: GET /requirements/intake answers 404.
    response = admin.get("/requirements/intake")
    assert response.status_code == 404, response.text


def test_session_start_with_only_repository_branch_and_task_passes_validation(admin):
    # FR-226: the three fields are the whole body. The service may not reach the repository
    # (no GitHub), so the documented refusals 404, 409 and 503 are accepted, never 422.
    body = {"repository": {"full_name": f"owner-{uuid.uuid4().hex[:8]}/repo", "branch": "main"},
            "task": "Document the API."}
    response = admin.post("/requirements/sessions", json=body)
    assert response.status_code in (201, 404, 409, 503), response.text
    if response.status_code == 201:
        created = response.json()
        assert created["session_id"].startswith("requirements-session-"), created
    else:
        assert isinstance(response.json()["detail"], str) and response.json()["detail"]


@pytest.mark.parametrize("extra", (
    {"kind": "docs"},
    {"kind": "new_service"},
    {"answers": []},
    {"answers": ["A service"]},
), ids=("kind-docs", "kind-new-service", "answers-empty", "answers-given"))
def test_removed_kind_and_answers_fields_are_rejected(admin, extra):
    # FR-226 / CreateRequirementsSession: any other field, including kind or answers, is a 422.
    assert_unprocessable(admin.post("/requirements/sessions", json={**VALID, **extra}))


def test_unknown_field_is_rejected(admin):
    assert_unprocessable(admin.post("/requirements/sessions",
                                    json={**VALID, "priority": "high"}))


@pytest.mark.parametrize("missing", ("repository", "task"))
def test_repository_and_task_are_required(admin, missing):
    body = {key: value for key, value in VALID.items() if key != missing}
    assert_unprocessable(admin.post("/requirements/sessions", json=body))


@pytest.mark.parametrize("repository", (
    {"full_name": "owner/repo"},
    {"branch": "main"},
), ids=("no-branch", "no-full-name"))
def test_repository_needs_full_name_and_branch(admin, repository):
    assert_unprocessable(admin.post("/requirements/sessions",
                                    json={**VALID, "repository": repository}))


def test_task_over_10000_characters_is_rejected(admin):
    response = admin.post("/requirements/sessions", json={**VALID, "task": "x" * 10001})
    assert response.status_code in (413, 422), response.text
    assert response.json()["detail"]


def test_session_start_requires_sign_in(anonymous):
    response = anonymous.post("/requirements/sessions", json=VALID)
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"
