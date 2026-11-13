"""Task flows (FR-125 to FR-143) as far as the service answers without Temporal, an LLM
provider or GitHub: input validation of the intake that starts a task flow, and the shape
of whatever executions GET /workflows returns (200), or its documented refusal (503)."""
import uuid

import pytest

STAGE_TYPES = {"RequirementsWorkflow", "ArchitectureWorkflow", "ApiDesignWorkflow",
               "DeliveryWorkflow"}
STAGE_ONLY_STATUSES = {"pending", "skipped"}


def listing(admin, **params):
    """GET /workflows; returns the executions, or None when Temporal is unavailable (503)."""
    response = admin.get("/workflows", params=params)
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 503:
        assert isinstance(body["detail"], str) and body["detail"], response.text
        return None
    assert isinstance(body["workflows"], list)
    return body["workflows"]


REPOSITORY = {"full_name": "owner/repo", "branch": "main"}


# Starting a task flow (FR-125, FR-128): the intake is validated before anything starts.

@pytest.mark.parametrize("body", (
    {"kind": "docs", "answers": [], "task": "Do it."},
    {"repository": REPOSITORY, "answers": [], "task": "Do it."},
    {"repository": REPOSITORY, "kind": "docs", "task": "Do it."},
    {"repository": REPOSITORY, "kind": "docs", "answers": []},
    {"repository": {"full_name": "owner/repo"}, "kind": "docs", "answers": [],
     "task": "Do it."},
    {"repository": {"branch": "main"}, "kind": "docs", "answers": [], "task": "Do it."},
    {"repository": "owner/repo", "kind": "docs", "answers": [], "task": "Do it."},
    {"repository": REPOSITORY, "kind": "docs", "answers": "none", "task": "Do it."},
    {"repository": REPOSITORY, "kind": "docs", "answers": [], "task": 42},
), ids=("no-repository", "no-kind", "no-answers", "no-task", "no-branch", "no-full-name",
        "repository-not-an-object", "answers-not-a-list", "task-not-a-string"))
def test_starting_a_requirements_session_validates_the_intake(admin, body):
    response = admin.post("/requirements/sessions", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"], response.text


# The task flow in GET /workflows (FR-125, FR-128, FR-129, FR-135, FR-139, FR-143)

def test_task_workflow_type_lists_only_top_level_task_flows(admin):
    executions = listing(admin, type="TaskWorkflow")
    if executions is None:
        return
    for item in executions:
        assert item["workflow_type"] == "TaskWorkflow", item
        # The top-level flow of a task has no parent and is not an earlier stage of another.
        assert item.get("parent_workflow_id") is None, item
        assert item["linked"] is False, item
        assert not item["workflow_id"].startswith(
            ("requirements-session-", "architecture-session-", "api-design-session-")), item
        assert isinstance(item["name"], str), item
        assert item["repository"]["full_name"] and item["branch"], item
        assert item["repository"]["branch"] == item["branch"], item
        assert item["status"] not in STAGE_ONLY_STATUSES, item


def test_every_execution_carries_the_task_flow_fields(admin):
    executions = listing(admin)
    if executions is None:
        return
    for item in executions:
        for field in ("repository", "branch", "onboarding_id", "name", "linked",
                      "waiting_for_user", "unresolved_findings"):
            assert field in item, (field, item)
        assert isinstance(item["linked"], bool), item
        assert item["waiting_for_user"] is None or isinstance(item["waiting_for_user"], bool)
        assert (item["unresolved_findings"] is None
                or isinstance(item["unresolved_findings"], bool)), item
        if item["workflow_type"] != "TaskWorkflow":
            assert item["name"] is None, item
        if item["workflow_type"] in STAGE_TYPES | {"TaskWorkflow"}:
            assert item["repository"]["full_name"] and item["branch"], item
        # pending and skipped belong to the stage children of a task flow only.
        if item["status"] in STAGE_ONLY_STATUSES:
            assert item["workflow_type"] in STAGE_TYPES, item
            assert item.get("parent_workflow_id"), item


def test_top_level_lists_task_flows_but_never_a_stage_of_a_task(admin):
    executions = listing(admin, top_level="true")
    if executions is None:
        return
    for item in executions:
        assert item.get("parent_workflow_id") is None, item
        # A DeliveryWorkflow may be top-level: a fix round on a closed flow's pull
        # request runs as its own top-level flow (FR-141/FR-142).
        assert item["workflow_type"] not in ("ArchitectureWorkflow",
                                             "ApiDesignWorkflow"), item
        assert item["status"] not in STAGE_ONLY_STATUSES, item
        assert item["linked"] is False, item


def test_parent_lists_the_direct_stages_of_a_task_flow(admin):
    task_flows = listing(admin, type="TaskWorkflow")
    if task_flows is None:
        return
    for task_flow in task_flows[:3]:
        children = listing(admin, parent=task_flow["workflow_id"])
        assert children is not None
        for child in children:
            assert child["workflow_type"] in STAGE_TYPES, child
            if child["linked"]:
                # An earlier stage of the original task that a fork or fix round links to
                # keeps its own task flow as its parent (FR-140).
                assert child.get("parent_workflow_id") != task_flow["workflow_id"], child
            else:
                assert child.get("parent_workflow_id") == task_flow["workflow_id"], child


def test_parent_of_an_unknown_flow_lists_nothing(admin):
    executions = listing(admin, parent=f"task-{uuid.uuid4().hex}")
    if executions is not None:
        assert executions == []
