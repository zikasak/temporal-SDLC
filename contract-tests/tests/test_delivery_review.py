"""Delivery and code review endpoints, as far as the service answers without Temporal,
an LLM provider or GitHub: sign-in, input validation and unknown sessions."""
import uuid

import pytest

SESSION_PREFIXES = ("/requirements/sessions/requirements-session-",
                    "/api-design/sessions/api-design-session-")


def unknown_session(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


@pytest.fixture(params=SESSION_PREFIXES, ids=("requirements", "api-design"))
def session_prefix(request) -> str:
    return request.param


def assert_error_detail(response) -> None:
    assert isinstance(response.json().get("detail"), (str, list)), response.text
    assert response.json()["detail"]


def test_review_endpoints_require_sign_in(anonymous, session_prefix):
    path = unknown_session(session_prefix)
    calls = (
        ("/delivery/findings/accept", None),
        ("/delivery/findings/instructions", {"instructions": "Fix the findings."}),
        ("/generation/retry", {"stage": "review"}),
    )
    for suffix, body in calls:
        response = anonymous.post(f"{path}{suffix}", json=body)
        assert response.status_code == 401, suffix
        assert response.headers["www-authenticate"] == "Bearer", suffix


def test_accepting_findings_of_an_unknown_session_is_not_found(admin, session_prefix):
    response = admin.post(f"{unknown_session(session_prefix)}/delivery/findings/accept")
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)


def test_instructions_for_an_unknown_session_are_not_found(admin, session_prefix):
    response = admin.post(f"{unknown_session(session_prefix)}/delivery/findings/instructions",
                          json={"instructions": "Keep the tests, change the handler."})
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)


@pytest.mark.parametrize("body", (
    {},
    {"instructions": ""},
    {"instructions": "   \n\t "},
    {"instructions": 42},
), ids=("missing", "empty", "blank", "not-a-string"))
def test_findings_instructions_are_validated(admin, session_prefix, body):
    response = admin.post(f"{unknown_session(session_prefix)}/delivery/findings/instructions",
                          json=body)
    assert response.status_code == 422
    assert_error_detail(response)


def test_findings_instructions_are_limited_to_10000_characters(admin, session_prefix):
    response = admin.post(f"{unknown_session(session_prefix)}/delivery/findings/instructions",
                          json={"instructions": "x" * 10001})
    assert response.status_code in (413, 422)
    assert_error_detail(response)


def test_review_stage_retry_of_an_unknown_session_is_not_found(admin, session_prefix):
    response = admin.post(f"{unknown_session(session_prefix)}/generation/retry",
                          json={"stage": "review"})
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)


@pytest.mark.parametrize("body", ({}, {"stage": "deploy"}, {"stage": "REVIEW"}),
                         ids=("missing", "unknown-stage", "wrong-case"))
def test_retry_stage_must_be_a_documented_stage(admin, session_prefix, body):
    response = admin.post(f"{unknown_session(session_prefix)}/generation/retry", json=body)
    assert response.status_code == 422
    assert_error_detail(response)


def test_delivery_answers_need_a_workflow_id(admin, session_prefix):
    response = admin.post(f"{unknown_session(session_prefix)}/delivery/answers",
                          json={"answers": ["yes"]})
    assert response.status_code == 422
    assert_error_detail(response)


def test_delivery_answers_for_an_unknown_session_are_not_found(admin, session_prefix):
    response = admin.post(f"{unknown_session(session_prefix)}/delivery/answers",
                          json={"workflow_id": f"stage-{uuid.uuid4().hex}", "answers": ["yes"]})
    assert response.status_code == 404
