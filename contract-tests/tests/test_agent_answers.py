"""Answers to a paused delivery agent (FR-14, FR-185, FR-190, TR-8): the body of
POST .../delivery/answers is `AgentAnswersInput`, a `workflow_id` string and a list of answer
strings. Delivery progress and agent questions need GitHub, Temporal and an LLM, so only input
validation and unknown sessions are observable here."""
import uuid

import pytest

PREFIXES = ("/requirements/sessions/requirements-session-",
            "/api-design/sessions/api-design-session-",
            "/onboarding/sessions/onboarding-session-")


@pytest.fixture(params=PREFIXES, ids=("requirements", "api-design", "onboarding"))
def delivery_answers_path(request) -> str:
    return f"{request.param}{uuid.uuid4().hex}/delivery/answers"


@pytest.mark.parametrize("body", (
    {"workflow_id": "stage-x", "answers": "yes"},
    {"workflow_id": "stage-x", "answers": [1, 2]},
    {"workflow_id": "stage-x", "answers": None},
    {"workflow_id": 7, "answers": ["yes"]},
    {"workflow_id": None, "answers": ["yes"]},
    {},
), ids=("answers-not-a-list", "answers-not-strings", "answers-null", "workflow-id-number",
        "workflow-id-null", "empty-object"))
def test_agent_answers_must_match_the_documented_body(admin, delivery_answers_path, body):
    response = admin.post(delivery_answers_path, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


def test_agent_answers_must_be_a_json_object(admin, delivery_answers_path):
    response = admin.post(delivery_answers_path, json=["yes"])
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


def test_agent_answers_of_an_unknown_session_are_not_found(admin, delivery_answers_path):
    response = admin.post(delivery_answers_path,
                          json={"workflow_id": f"stage-{uuid.uuid4().hex}", "answers": ["Yes", "No"]})
    assert response.status_code == 404, response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


def test_agent_answers_require_sign_in(anonymous, delivery_answers_path):
    response = anonymous.post(delivery_answers_path,
                              json={"workflow_id": "stage-x", "answers": ["yes"]})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
