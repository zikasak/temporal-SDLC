"""Task flows in GET /workflows and the start of a task (FR-125 to FR-143), as far as the
service answers without Temporal, an LLM provider or GitHub: the TaskWorkflow type and the new
fields are checked on whatever executions come back (nothing when Temporal is unavailable and
503 is answered), and starting a task is refused when the repository cannot be reached."""
import uuid

import pytest

STAGE_TYPES = {"ArchitectureWorkflow", "ApiDesignWorkflow", "DeliveryWorkflow"}


def listing(admin, **params):
    """The executions of GET /workflows, or None when Temporal is unavailable (503)."""
    response = admin.get("/workflows", params=params)
    assert response.status_code in (200, 503), response.text
    if response.status_code == 503:
        assert isinstance(response.json()["detail"], str) and response.json()["detail"]
        return None
    return response.json()["workflows"]


def test_task_workflow_is_a_documented_type(admin):
    items = listing(admin, type="TaskWorkflow")
    for item in items or ():
        assert item["workflow_type"] == "TaskWorkflow", item
        # A task flow is the top-level flow of its task.
        assert item["parent_workflow_id"] is None, item
        assert item["repository"]["full_name"] and item["branch"], item
        assert item["repository"]["branch"] == item["branch"], item
        assert isinstance(item["name"], str) and item["name"].strip(), item
        assert item["linked"] is False, item


def test_every_execution_carries_the_task_flow_fields(admin):
    for item in listing(admin) or ():
        assert "name" in item and "linked" in item, item
        assert "waiting_for_user" in item and "unresolved_findings" in item, item
        assert item["linked"] in (True, False), item
        assert item["waiting_for_user"] in (True, False, None), item
        assert item["unresolved_findings"] in (True, False, None), item
        if item["workflow_type"] != "TaskWorkflow":
            assert item["name"] is None, item


def test_statuses_pending_and_skipped_belong_to_stages_of_a_task_flow(admin):
    for item in listing(admin) or ():
        if item["status"] in ("pending", "skipped"):
            assert item["workflow_type"] in STAGE_TYPES | {"RequirementsWorkflow"}, item
            assert item["parent_workflow_id"], item


def test_stages_of_a_task_are_never_top_level(admin):
    for item in listing(admin, top_level="true") or ():
        assert item["workflow_type"] not in STAGE_TYPES, item
        assert item["parent_workflow_id"] is None, item
        assert item["linked"] is False, item


def test_top_level_task_listing_combines_with_the_type(admin):
    items = listing(admin, type="TaskWorkflow", top_level="true")
    for item in items or ():
        assert item["workflow_type"] == "TaskWorkflow", item


def test_children_of_an_unknown_flow_are_none_or_unavailable(admin):
    parent = f"task-{uuid.uuid4().hex}"
    assert not listing(admin, parent=parent)


def test_parent_listing_never_returns_the_parent_itself(admin):
    parent = f"task-{uuid.uuid4().hex}"
    for item in listing(admin, parent=parent) or ():
        assert item["workflow_id"] != parent, item
        # Direct children carry the parent; linked earlier stages are marked.
        assert item["parent_workflow_id"] == parent or item["linked"] is True, item


def test_parent_and_top_level_cannot_be_combined(admin):
    for top_level in ("true", "false"):
        response = admin.get("/workflows", params={
            "parent": f"task-{uuid.uuid4().hex}", "top_level": top_level})
        assert response.status_code == 422, response.text
        assert response.json()["detail"]


# Starting a task (FR-125)

def test_starting_a_task_is_refused_when_the_repository_is_unreachable(admin):
    intake = admin.get("/requirements/intake")
    assert intake.status_code == 200, intake.text
    questions = intake.json()["questions"]["docs"]
    answers = [question["options"][0] for question in questions]
    response = admin.post("/requirements/sessions", json={
        "repository": {"full_name": f"owner/none-{uuid.uuid4().hex[:10]}", "branch": "main"},
        "kind": "docs", "answers": answers, "task": "Document the public endpoints."})
    # No GitHub App is connected, so no task flow can start.
    assert response.status_code in (404, 409, 503), response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


@pytest.mark.parametrize("answers", ("yes", [1, 2], None), ids=("string", "numbers", "null"))
def test_task_intake_answers_must_be_a_list_of_strings(admin, answers):
    response = admin.post("/requirements/sessions", json={
        "repository": {"full_name": "owner/repo", "branch": "main"},
        "kind": "docs", "answers": answers, "task": "Document it."})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]
