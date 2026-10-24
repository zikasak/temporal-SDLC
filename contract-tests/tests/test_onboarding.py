"""Onboarding endpoints (FR-56 to FR-66), as far as the service answers without Temporal, an
LLM provider or GitHub: sign-in, input validation, unknown onboardings and the workflow
listing type."""
import uuid

import pytest


def onboarding() -> str:
    return f"/onboarding/sessions/onboarding-session-{uuid.uuid4().hex}"


def assert_detail(response) -> None:
    assert response.json()["detail"], response.text


VALID_BODIES = {
    "answers": {"answers": ["Yes"]},
    "checkout": None,
    "contract-decision": {"replace_contract": True},
    "delivery/answers": {"workflow_id": "stage-x", "answers": ["yes"]},
    "delivery/findings/accept": None,
    "delivery/findings/instructions": {"instructions": "Fix the findings."},
    "finalize": None,
    "fork": {"message_index": 0},
    "generation/retry": {"stage": "tests"},
    "messages": {"message": "The language is Go."},
    "retry": None,
}


@pytest.mark.parametrize("suffix", sorted(VALID_BODIES))
def test_onboarding_actions_require_sign_in(anonymous, suffix):
    response = anonymous.post(f"{onboarding()}/{suffix}", json=VALID_BODIES[suffix])
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_reading_an_onboarding_requires_sign_in(anonymous):
    response = anonymous.get(onboarding())
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_starting_an_onboarding_requires_sign_in(anonymous):
    response = anonymous.post("/onboarding/sessions", json={
        "repository": {"full_name": "owner/repo", "branch": "main"}})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("suffix", sorted(VALID_BODIES))
def test_actions_on_an_unknown_onboarding_are_not_found(admin, suffix):
    response = admin.post(f"{onboarding()}/{suffix}", json=VALID_BODIES[suffix])
    assert response.status_code == 404, response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


def test_unknown_onboarding_is_not_found_with_a_detail(admin):
    response = admin.get(onboarding())
    assert response.status_code == 404, response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


def test_malformed_onboarding_id_is_rejected_with_a_detail(admin):
    # The contract leaves open whether a malformed ID is "malformed" (422) or "unknown" (404).
    response = admin.get("/onboarding/sessions/onboarding-session-unknown")
    assert response.status_code in (404, 422), response.text
    assert response.json()["detail"], response.text


# Starting an onboarding (FR-56)

@pytest.mark.parametrize("body", (
    {},
    {"repository": None},
    {"repository": {"full_name": "owner/repo"}},
    {"repository": {"branch": "main"}},
    {"repository": "owner/repo"},
), ids=("no-repository", "null-repository", "no-branch", "no-full-name", "not-an-object"))
def test_onboarding_needs_a_repository_and_branch(admin, body):
    response = admin.post("/onboarding/sessions", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]


def test_onboarding_cannot_start_without_a_reachable_github_repository(admin):
    # No GitHub App is connected to the service under test, so nothing can be onboarded.
    response = admin.post("/onboarding/sessions", json={
        "repository": {"full_name": f"owner/none-{uuid.uuid4().hex[:10]}", "branch": "main"}})
    assert response.status_code in (404, 409, 503), response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


# Input validation of the onboarding conversation (FR-58)

@pytest.mark.parametrize("message", ("", "   \n", "x" * 10001), ids=("empty", "blank", "too-long"))
def test_onboarding_messages_are_one_to_10000_characters(admin, message):
    response = admin.post(f"{onboarding()}/messages", json={"message": message})
    assert response.status_code in (413, 422)
    assert response.json()["detail"]


def test_onboarding_message_is_required(admin):
    assert admin.post(f"{onboarding()}/messages", json={}).status_code == 422


@pytest.mark.parametrize("body", ({}, {"answers": "yes"}, {"answers": [1, 2]}),
                         ids=("missing", "not-a-list", "not-strings"))
def test_onboarding_answers_must_be_a_list_of_strings(admin, body):
    response = admin.post(f"{onboarding()}/answers", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]


@pytest.mark.parametrize("body", ({}, {"message_index": "first"}, {"message_index": None}),
                         ids=("missing", "not-an-integer", "null"))
def test_onboarding_fork_needs_a_message_index(admin, body):
    response = admin.post(f"{onboarding()}/fork", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]


# Contract decision (FR-61)

@pytest.mark.parametrize("body", ({}, {"replace_contract": None}, {"replace_contract": "maybe"},
                                  {"replace_contract": [True]}),
                         ids=("missing", "null", "not-a-boolean", "list"))
def test_contract_decision_needs_a_boolean(admin, body):
    response = admin.post(f"{onboarding()}/contract-decision", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]


@pytest.mark.parametrize("replace", (True, False))
def test_contract_decision_of_an_unknown_onboarding_is_not_found(admin, replace):
    response = admin.post(f"{onboarding()}/contract-decision", json={"replace_contract": replace})
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)


# Delivery of the onboarding (FR-60, FR-62, FR-63, FR-65)

def test_onboarding_delivery_answers_need_a_workflow_id(admin):
    response = admin.post(f"{onboarding()}/delivery/answers", json={"answers": ["yes"]})
    assert response.status_code == 422
    assert response.json()["detail"]


def test_onboarding_delivery_answers_need_answers(admin):
    response = admin.post(f"{onboarding()}/delivery/answers",
                          json={"workflow_id": f"stage-{uuid.uuid4().hex}"})
    assert response.status_code == 422


@pytest.mark.parametrize("body", (
    {},
    {"instructions": ""},
    {"instructions": "  \n\t "},
    {"instructions": 42},
), ids=("missing", "empty", "blank", "not-a-string"))
def test_onboarding_findings_instructions_are_validated(admin, body):
    response = admin.post(f"{onboarding()}/delivery/findings/instructions", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]


def test_onboarding_findings_instructions_are_limited_to_10000_characters(admin):
    response = admin.post(f"{onboarding()}/delivery/findings/instructions",
                          json={"instructions": "x" * 10001})
    assert response.status_code in (404, 413, 422)
    assert response.json()["detail"]


@pytest.mark.parametrize("body", ({}, {"stage": "deploy"}, {"stage": "Tests"}),
                         ids=("missing", "unknown-stage", "wrong-case"))
def test_onboarding_retry_stage_must_be_a_documented_stage(admin, body):
    response = admin.post(f"{onboarding()}/generation/retry", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]


@pytest.mark.parametrize("stage", ("server", "tests", "documentation", "repository", "review"))
def test_every_documented_stage_reaches_the_onboarding_lookup(admin, stage):
    response = admin.post(f"{onboarding()}/generation/retry", json={"stage": stage})
    assert response.status_code == 404, response.text


# Listing onboardings (FR-66)

def test_workflow_listing_accepts_the_onboarding_type(admin):
    response = admin.get("/workflows", params={"type": "OnboardingWorkflow"})
    # 200 with Temporal, 503 without it; the type itself is a documented one.
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 200:
        assert all(item["workflow_type"] == "OnboardingWorkflow" for item in body["workflows"])
    else:
        assert isinstance(body["detail"], str) and body["detail"]
