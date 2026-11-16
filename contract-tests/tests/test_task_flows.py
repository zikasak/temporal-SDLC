"""Task flows in GET /workflows and the start of a task (FR-125 to FR-140, FR-143, NFR-7),
as far as the service answers without Temporal, an LLM provider or GitHub.

Without Temporal the listing answers 503, so the checks on listed executions only run when
the service has a workflow engine; they then hold for whatever executions come back."""
import uuid

import pytest

STAGE_TYPES = {"RequirementsWorkflow", "ArchitectureWorkflow", "ApiDesignWorkflow",
               "DeliveryWorkflow"}
CLOSED = {"completed", "failed", "cancelled", "terminated", "timed_out", "canceled"}


def list_workflows(admin, **params):
    """The first page of a listing, or None when the service has no workflow engine (503)."""
    response = admin.get("/workflows", params=params)
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 503:
        assert isinstance(body["detail"], str) and body["detail"]
        return None
    return body["workflows"]


@pytest.mark.parametrize("workflow_type", ("TaskWorkflow", "RequirementsWorkflow",
                                           "ApiDesignWorkflow", "DeliveryWorkflow"))
def test_workflow_listing_accepts_the_task_and_stage_types(admin, workflow_type):
    items = list_workflows(admin, type=workflow_type)
    for item in items or []:
        assert item["workflow_type"] == workflow_type, item


def test_task_flows_are_top_level_and_named(admin):
    items = list_workflows(admin, type="TaskWorkflow")
    for item in items or []:
        # FR-125, FR-128: a task flow has no parent and carries its name.
        assert item["parent_workflow_id"] is None, item
        assert isinstance(item["name"], str) and item["name"].strip(), item
        assert item["linked"] is False, item
        assert item["status"] not in ("pending", "skipped"), item


def test_executions_always_carry_the_task_flow_fields(admin):
    items = list_workflows(admin)
    for item in items or []:
        for field in ("name", "linked", "waiting_for_user", "unresolved_findings"):
            assert field in item, (field, item)
        if item["workflow_type"] != "TaskWorkflow":
            assert item["name"] is None, item
        assert isinstance(item["linked"], bool), item
        for flag in ("waiting_for_user", "unresolved_findings"):
            assert item[flag] is None or isinstance(item[flag], bool), item
        # FR-110: a closed flow never waits for the user.
        if item["status"] in CLOSED:
            assert item["waiting_for_user"] is not True, item
        # Only a stage of a task flow can be pending or skipped (FR-129, FR-135).
        if item["status"] in ("pending", "skipped"):
            assert item["parent_workflow_id"] is not None, item
            assert item["workflow_type"] in STAGE_TYPES, item


def test_top_level_listing_never_holds_a_stage_of_a_task(admin):
    items = list_workflows(admin, top_level="true")
    for item in items or []:
        assert item["parent_workflow_id"] is None, item
        assert item["linked"] is False, item
        assert item["status"] not in ("pending", "skipped"), item


def test_children_of_a_task_flow_are_its_stages_and_the_stages_it_links_to(admin):
    task_flows = list_workflows(admin, type="TaskWorkflow", top_level="true")
    for task in (task_flows or [])[:5]:
        children = list_workflows(admin, parent=task["workflow_id"])
        assert children is not None
        for child in children:
            assert child["workflow_type"] in STAGE_TYPES, child
            if child["linked"]:
                # An earlier stage of another task keeps its own task flow as parent.
                assert child["parent_workflow_id"] != task["workflow_id"], child
            else:
                assert child["parent_workflow_id"] == task["workflow_id"], child


def test_a_requirements_or_api_design_session_lists_no_children(admin):
    task_flows = list_workflows(admin, type="TaskWorkflow", top_level="true")
    for task in (task_flows or [])[:3]:
        for stage in list_workflows(admin, parent=task["workflow_id"]) or []:
            if stage["workflow_type"] in ("RequirementsWorkflow", "ApiDesignWorkflow"):
                # FR-131: a stage never lists another stage of the task as its child.
                assert list_workflows(admin, parent=stage["workflow_id"]) == [], stage


# Starting a task (FR-125)

def valid_task_body(admin, kind: str = "docs") -> dict:
    intake = admin.get("/requirements/intake")
    assert intake.status_code == 200, intake.text
    questions = intake.json()["questions"][kind]
    return {
        "repository": {"full_name": f"owner/none-{uuid.uuid4().hex[:10]}", "branch": "main"},
        "kind": kind,
        "answers": [item["options"][0] for item in questions],
        "task": "Document how the service is deployed.",
    }


@pytest.mark.parametrize("kind", ("new_service", "api_change", "bugfix", "docs"))
def test_a_task_cannot_start_in_a_repository_the_service_cannot_reach(admin, kind):
    # No GitHub App is connected, so there is no task flow to start.
    response = admin.post("/requirements/sessions", json=valid_task_body(admin, kind))
    assert response.status_code in (404, 409, 503), response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]
