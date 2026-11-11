"""Behaviour of the flow lifecycle and listing that the service answers without Temporal, an
LLM provider or GitHub: pull request webhook events are never trusted without the webhook
secret (FR-102 to FR-104), the boolean `top_level` filter (FR-76) and sign-in on every
session endpoint that other modules only call as the administrator."""
import json
import uuid

import pytest

EVENTS_PATH = "/connectors/github/events"


def pull_request_payload(action: str, merged: bool) -> dict:
    return {
        "action": action,
        "pull_request": {"number": 7, "state": "closed", "merged": merged,
                         "html_url": "https://github.com/owner/repo/pull/7",
                         "head": {"ref": "onboarding/main-0123abcd"},
                         "base": {"ref": "main"}},
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "maintainer"},
    }


@pytest.mark.parametrize("merged", (True, False), ids=("merged", "closed-without-merge"))
def test_unsigned_pull_request_close_event_is_rejected(anonymous, merged):
    # A forged "merged" or "closed" delivery must not be able to complete or cancel a flow.
    body = json.dumps(pull_request_payload("closed", merged)).encode()
    response = anonymous.post(EVENTS_PATH, content=body, headers={
        "Content-Type": "application/json", "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": str(uuid.uuid4())})
    assert response.status_code == 401, response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


def test_pull_request_event_signed_with_a_guessed_key_is_rejected(anonymous):
    import hashlib
    import hmac

    body = json.dumps(pull_request_payload("closed", True)).encode()
    for secret in (b"", b"secret", b"webhook-secret"):
        signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        response = anonymous.post(EVENTS_PATH, content=body, headers={
            "Content-Type": "application/json", "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": str(uuid.uuid4()), "X-Hub-Signature-256": signature})
        assert response.status_code == 401, (secret, response.text)


@pytest.mark.parametrize("value", ("maybe", "2", "yes please"))
def test_top_level_filter_must_be_a_boolean(admin, value):
    response = admin.get("/workflows", params={"top_level": value})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


@pytest.mark.parametrize("value", ("true", "false"))
def test_boolean_top_level_filter_is_not_rejected(admin, value):
    response = admin.get("/workflows", params={"top_level": value})
    # 200 with Temporal, 503 without it; never a validation error.
    assert response.status_code in (200, 503), response.text
    if response.status_code == 503:
        assert response.json()["detail"]
    elif value == "true":
        for item in response.json()["workflows"]:
            assert item["workflow_type"] != "DeliveryWorkflow", item


SESSION_BODIES = {
    "messages": {"message": "Hello"},
    "answers": {"answers": ["Yes"]},
    "fork": {"message_index": 0},
    "retry": None,
    "finalize": None,
    "delivery/answers": {"workflow_id": "stage-x", "answers": ["yes"]},
}


@pytest.mark.parametrize("prefix", ("/requirements/sessions/requirements-session-",
                                    "/api-design/sessions/api-design-session-"),
                         ids=("requirements", "api-design"))
@pytest.mark.parametrize("suffix", sorted(SESSION_BODIES))
def test_session_actions_require_sign_in(anonymous, prefix, suffix):
    response = anonymous.post(f"{prefix}{uuid.uuid4().hex}/{suffix}", json=SESSION_BODIES[suffix])
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("prefix", ("/requirements/sessions/requirements-session-",
                                    "/api-design/sessions/api-design-session-"),
                         ids=("requirements", "api-design"))
def test_session_documents_require_sign_in(anonymous, prefix):
    response = anonymous.get(f"{prefix}{uuid.uuid4().hex}/document")
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"


def test_requirements_checkout_requires_sign_in(anonymous):
    response = anonymous.post(
        f"/requirements/sessions/requirements-session-{uuid.uuid4().hex}/checkout")
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"
