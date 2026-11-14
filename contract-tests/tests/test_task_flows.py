"""Task flows (FR-125 to FR-143) and the sign-in rules of the chat endpoints, as far as the
service answers without Temporal, an LLM provider or GitHub behind it."""
import uuid

import pytest


def stage_id() -> str:
    return f"requirements-session-{uuid.uuid4().hex}"


@pytest.mark.parametrize("params", (
    {"type": "TaskWorkflow"},
    {"type": "TaskWorkflow", "top_level": "true"},
    {"type": "TaskWorkflow", "repository": "Some-Owner/Some-Repo", "branch": "main"},
    {"parent": stage_id()},
    {"parent": stage_id(), "type": "DeliveryWorkflow", "branch": "main"},
), ids=("type", "type-and-top-level", "type-repository-branch", "parent", "parent-and-filters"))
def test_task_flow_listing_is_not_rejected(admin, params):
    response = admin.get("/workflows", params=params)
    # 200 with Temporal, 503 without it; TaskWorkflow is a documented type and these
    # filters combine, so never a validation error.
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 503:
        assert isinstance(body["detail"], str) and body["detail"]
        return
    for item in body["workflows"]:
        # The fields every execution gains are always present, null when not applicable.
        for field in ("repository", "branch", "onboarding_id", "name", "linked",
                      "waiting_for_user", "unresolved_findings"):
            assert field in item, (field, item)
        if params.get("type") == "TaskWorkflow":
            assert item["workflow_type"] == "TaskWorkflow", item
            # A task flow is the top of its task and carries its short name.
            assert not item.get("parent_workflow_id"), item
            assert isinstance(item["name"], str) and item["name"].strip(), item
            assert item["repository"]["full_name"] and item["branch"], item
            assert item["repository"]["branch"] == item["branch"], item
        if "parent" not in params:
            assert item["linked"] in (False, None), item


def test_parent_cannot_be_combined_with_top_level(admin):
    response = admin.get("/workflows", params={"top_level": "true", "parent": stage_id()})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


@pytest.mark.parametrize("kind", ("new_service", "api_change", "bugfix", "docs"))
def test_requirements_session_on_an_unreachable_repository_is_refused(admin, kind):
    # No GitHub App is connected to the service under test, so no task flow can start.
    response = admin.post("/requirements/sessions", json={
        "repository": {"full_name": f"owner/none-{uuid.uuid4().hex[:10]}", "branch": "main"},
        "kind": kind, "answers": [], "task": "Add a health endpoint."})
    assert response.status_code in (404, 409, 503), response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


def test_intake_kinds_have_labels_and_questions_have_options(admin):
    intake = admin.get("/requirements/intake").json()
    for item in intake["kinds"]:
        assert isinstance(item["label"], str) and item["label"].strip(), item
    assert len({item["kind"] for item in intake["kinds"]}) == len(intake["kinds"])
    for kind, questions in intake["questions"].items():
        for item in questions:
            assert isinstance(item["question"], str) and item["question"].strip(), kind
            assert isinstance(item["options"], list), kind


@pytest.mark.parametrize("method,suffix,body", (
    ("GET", "", None),
    ("POST", "/retry", None),
    ("POST", "/fork", {"message_index": 0}),
    ("GET", f"/documents/{uuid.uuid4().hex}", None),
), ids=("get", "retry", "fork", "document"))
def test_chat_requests_require_sign_in(anonymous, method, suffix, body):
    response = anonymous.request(method, f"/llm/requests/llm-request-{uuid.uuid4().hex}{suffix}",
                                 json=body)
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"
