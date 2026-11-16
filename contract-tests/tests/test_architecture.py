"""Architecture session endpoints (FR-78 to FR-88), as far as the service answers without
Temporal, an LLM provider or GitHub. Architecture sessions are started only by a finalized
requirements session, so the suite covers sign-in, input validation and unknown sessions."""
import uuid

import pytest


def architecture() -> str:
    return f"/architecture/sessions/architecture-session-{uuid.uuid4().hex}"


def assert_detail(response) -> None:
    assert response.json()["detail"], response.text


VALID_BODIES = {
    "answers": {"answers": ["Yes"]},
    "finalize": None,
    "fork": {"message_index": 0},
    "messages": {"message": "Use a message queue between the components."},
    "retry": None,
    "skip": None,
}


@pytest.mark.parametrize("suffix", sorted(VALID_BODIES))
def test_architecture_actions_require_sign_in(anonymous, suffix):
    response = anonymous.post(f"{architecture()}/{suffix}", json=VALID_BODIES[suffix])
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("path", ("", "/document", "/document?format=json"))
def test_reading_an_architecture_session_requires_sign_in(anonymous, path):
    response = anonymous.get(f"{architecture()}{path}")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_architecture_sessions_cannot_be_started_directly(admin):
    # Only the requirements session starts one: there is no POST /architecture/sessions.
    response = admin.post("/architecture/sessions", json={
        "requirements_session_id": f"requirements-session-{uuid.uuid4().hex}"})
    assert response.status_code in (404, 405), response.text


def test_unknown_architecture_session_is_not_found_with_a_detail(admin):
    response = admin.get(architecture())
    assert response.status_code == 404, response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


@pytest.mark.parametrize("suffix", sorted(VALID_BODIES))
def test_actions_on_an_unknown_architecture_session_are_not_found(admin, suffix):
    response = admin.post(f"{architecture()}/{suffix}", json=VALID_BODIES[suffix])
    assert response.status_code == 404, response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


@pytest.mark.parametrize("params", ({}, {"format": "md"}, {"format": "json"}),
                         ids=("default", "md", "json"))
def test_document_of_an_unknown_architecture_session_is_not_found(admin, params):
    response = admin.get(f"{architecture()}/document", params=params)
    assert response.status_code == 404, response.text
    assert_detail(response)


@pytest.mark.parametrize("fmt", ("yaml", "pdf", "MD", ""))
def test_document_format_must_be_md_or_json(admin, fmt):
    response = admin.get(f"{architecture()}/document", params={"format": fmt})
    # The contract does not say whether the format or the session is checked first.
    assert response.status_code in (404, 422), response.text
    assert_detail(response)


@pytest.mark.parametrize("message", ("", "   \n", "x" * 10001), ids=("empty", "blank", "too-long"))
def test_architecture_messages_are_one_to_10000_characters(admin, message):
    response = admin.post(f"{architecture()}/messages", json={"message": message})
    assert response.status_code in (413, 422), response.text
    assert_detail(response)


def test_architecture_message_is_required(admin):
    response = admin.post(f"{architecture()}/messages", json={})
    assert response.status_code == 422
    assert_detail(response)


@pytest.mark.parametrize("body", ({}, {"answers": "yes"}, {"answers": [1, 2]}),
                         ids=("missing", "not-a-list", "not-strings"))
def test_architecture_answers_must_be_a_list_of_strings(admin, body):
    response = admin.post(f"{architecture()}/answers", json=body)
    assert response.status_code == 422
    assert_detail(response)


@pytest.mark.parametrize("body", ({}, {"message_index": "first"}, {"message_index": None}),
                         ids=("missing", "not-an-integer", "null"))
def test_architecture_fork_needs_a_message_index(admin, body):
    response = admin.post(f"{architecture()}/fork", json=body)
    assert response.status_code == 422
    assert_detail(response)


# Architecture stage in the workflow listing (FR-87, FR-90, FR-91)

def test_workflow_listing_accepts_the_architecture_type(admin):
    response = admin.get("/workflows", params={"type": "ArchitectureWorkflow"})
    # 200 with Temporal, 503 without it; the type itself is a documented one, not a 422.
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 503:
        assert isinstance(body["detail"], str) and body["detail"]
        return
    for item in body["workflows"]:
        assert item["workflow_type"] == "ArchitectureWorkflow", item
        assert item["workflow_id"].startswith("architecture-session-"), item
        # FR-125/FR-132: a direct child of its task flow, never of another stage flow.
        parent = item["parent_workflow_id"]
        assert parent is None or isinstance(parent, str), item
        if parent is not None:
            assert parent
            assert not parent.startswith(("requirements-session-", "architecture-session-",
                                          "api-design-session-")), item
