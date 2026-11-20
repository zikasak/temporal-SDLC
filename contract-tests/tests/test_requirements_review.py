"""Requirements review endpoints (FR-144 to FR-147, FR-153, FR-157, FR-158, FR-168, FR-169), as
far as the service answers without Temporal, an LLM provider or GitHub. A review round runs only
on a finalized session of a task or an onboarding, which needs all three, so the suite covers
sign-in, input validation (the `force` flag of finalize, answers) and unknown sessions."""
import uuid

import pytest

PREFIXES = {
    "requirements": "/requirements/sessions/requirements-session-",
    "onboarding": "/onboarding/sessions/onboarding-session-",
}


def session_path(kind: str) -> str:
    return f"{PREFIXES[kind]}{uuid.uuid4().hex}"


@pytest.fixture(params=sorted(PREFIXES))
def kind(request) -> str:
    return request.param


def test_retrying_a_review_round_requires_sign_in(anonymous, kind):
    response = anonymous.post(f"{session_path(kind)}/review/retry")
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"


def test_retrying_the_review_of_an_unknown_session_is_not_found(admin, kind):
    response = admin.post(f"{session_path(kind)}/review/retry")
    assert response.status_code == 404, response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


@pytest.mark.parametrize("force", ("true", "false"))
def test_finalizing_an_unknown_session_with_force_is_not_found(admin, kind, force):
    response = admin.post(f"{session_path(kind)}/finalize", params={"force": force})
    assert response.status_code == 404, response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


@pytest.mark.parametrize("force", ("maybe", "2", "yes please"))
def test_force_must_be_a_boolean(admin, kind, force):
    response = admin.post(f"{session_path(kind)}/finalize", params={"force": force})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


def test_finalizing_with_force_requires_sign_in(anonymous, kind):
    response = anonymous.post(f"{session_path(kind)}/finalize", params={"force": "true"})
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"


# The contradiction questions of a review round are answered through POST .../answers.

@pytest.mark.parametrize("prefix", ("requirements", "api-design"))
@pytest.mark.parametrize("body", ({}, {"answers": None}, {"answers": [1, 2]},
                                  {"answers": {"0": "yes"}}),
                         ids=("missing", "null", "not-strings", "object"))
def test_answers_must_be_a_list_of_strings(admin, prefix, body):
    path = f"/{prefix}/sessions/{prefix}-session-{uuid.uuid4().hex}/answers"
    response = admin.post(path, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


@pytest.mark.parametrize("prefix", ("requirements", "api-design"))
def test_answers_to_an_unknown_session_are_not_found(admin, prefix):
    path = f"/{prefix}/sessions/{prefix}-session-{uuid.uuid4().hex}/answers"
    response = admin.post(path, json={"answers": ["Yes"]})
    assert response.status_code == 404, response.text
    assert response.json()["detail"]
