"""Task flows in GET /workflows (FR-125, FR-127 to FR-129, FR-131, FR-136, FR-139, FR-143), as
far as the service answers without Temporal, an LLM provider or GitHub. Task flows are started
by a requirements session, which needs all three, so the suite checks the listing's filters:
what is rejected before Temporal is contacted (422), what is not rejected (200, or 503 without
Temporal) and, when Temporal does answer, what the executions returned must look like."""
import uuid

import pytest

# A delivery or codegen flow may legitimately be top-level (a delivery of a closed pull request
# fixed by an onboarding, or one from before task flows existed), so only these two are banned.
STAGE_TYPES = {"ArchitectureWorkflow", "ApiDesignWorkflow"}
CLOSED = {"completed", "cancelled", "failed", "terminated", "timed_out", "skipped"}


def task_flow_id() -> str:
    # The contract does not name the format of a task flow's ID; any well-formed workflow ID
    # must be accepted by the parent filter.
    return f"task-{uuid.uuid4().hex}"


def listed(admin, **params):
    """GET /workflows: 200 with Temporal, 503 with a detail without it. Returns the executions
    or None when Temporal is unavailable."""
    response = admin.get("/workflows", params=params)
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 503:
        assert isinstance(body["detail"], str) and body["detail"]
        return None
    return body["workflows"]


def test_task_workflow_is_a_documented_type_listing_only_task_flows(admin):
    executions = listed(admin, type="TaskWorkflow")
    for item in executions or []:
        assert item["workflow_type"] == "TaskWorkflow", item
        # A task flow is top-level, never a stage of another flow, and never a linked stage.
        assert item["parent_workflow_id"] is None, item
        assert item["linked"] is False, item
        assert isinstance(item["name"], str) and item["name"], item
        assert item["repository"]["full_name"], item
        assert item["branch"] == item["repository"]["branch"], item
        assert item["onboarding_id"] is None, item
        assert item["unresolved_findings"] in (True, False, None), item
        assert item["waiting_for_user"] in (True, False, None), item


def test_top_level_listing_has_no_stage_of_a_task(admin):
    for item in listed(admin, top_level="true", type="TaskWorkflow") or []:
        assert item["workflow_type"] == "TaskWorkflow", item
        assert item["parent_workflow_id"] is None, item
    for item in listed(admin, top_level="true") or []:
        assert item["parent_workflow_id"] is None, item
        assert item["workflow_type"] not in STAGE_TYPES, item


def test_top_level_combines_with_the_repository_and_branch_filters(admin):
    executions = listed(admin, top_level="true", repository="Some-Owner/Some-Repo",
                        branch="feature/x")
    for item in executions or []:
        assert item["parent_workflow_id"] is None, item
        assert item["repository"]["full_name"].lower() == "some-owner/some-repo", item
        assert item["branch"] == "feature/x", item


def test_parent_of_a_task_flow_lists_only_its_children_and_linked_stages(admin):
    parent = task_flow_id()
    for item in listed(admin, parent=parent) or []:
        # Its own stages have it as parent; a linked earlier stage keeps its own task flow.
        assert item["parent_workflow_id"] == parent or item["linked"] is True, item
        assert item["workflow_type"] != "TaskWorkflow", item


def test_parent_combines_with_type_and_the_other_filters(admin):
    executions = listed(admin, parent=task_flow_id(), type="ArchitectureWorkflow",
                        repository="Some-Owner/Some-Repo", branch="main")
    for item in executions or []:
        assert item["workflow_type"] == "ArchitectureWorkflow", item
        assert item["branch"] == "main", item


@pytest.mark.parametrize("extra", (
    {"repository": "not-a-repository"},
    {"branch": ""},
    {"origin": "somewhere-else"},
), ids=("repository", "branch", "origin"))
def test_parent_does_not_excuse_a_malformed_filter(admin, extra):
    response = admin.get("/workflows", params={"parent": task_flow_id(), **extra})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


def test_top_level_does_not_excuse_a_malformed_filter(admin):
    response = admin.get("/workflows", params={"top_level": "true", "repository": "justaname"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


@pytest.mark.parametrize("params", (
    {"parent": "task-x", "top_level": "true"},
    {"parent": "", "top_level": "true"},
), ids=("parent-and-top-level", "empty-parent-and-top-level"))
def test_parent_cannot_be_combined_with_top_level_whatever_the_type(admin, params):
    response = admin.get("/workflows", params={"type": "TaskWorkflow", **params})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


@pytest.mark.parametrize("params", (
    {"top_level": "true"},
    {"parent": "task-x"},
    {"type": "TaskWorkflow"},
), ids=("top-level", "parent", "task-type"))
def test_listing_task_flows_requires_sign_in(anonymous, params):
    response = anonymous.get("/workflows", params=params)
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"


def test_executions_always_carry_the_flow_fields_and_keep_the_old_ones(admin):
    for item in listed(admin) or []:
        for old in ("workflow_id", "run_id", "workflow_type", "status", "task_queue", "start_time"):
            assert isinstance(item[old], str) and item[old], item
        for new in ("repository", "branch", "onboarding_id", "name", "linked", "waiting_for_user",
                    "unresolved_findings"):
            assert new in item, (new, item)
        assert isinstance(item["linked"], bool), item
        if item["workflow_type"] != "TaskWorkflow":
            assert item["name"] is None, item
        if item["status"] == "pending":
            # Only a stage of a task flow waits for the stages it depends on.
            assert item["parent_workflow_id"], item
            assert item["workflow_type"] != "TaskWorkflow", item
        if item["status"] in CLOSED:
            # A closed flow no longer waits for the user.
            assert item["waiting_for_user"] in (False, None), item


# Starting a task: the intake's types are checked before any service is contacted (FR-125).

@pytest.mark.parametrize("change", (
    {"answers": "yes"},
    {"answers": [1, 2]},
    {"task": 42},
    {"task": None},
    {"kind": None},
    {"repository": "owner/repo"},
    {"repository": {"full_name": "owner/repo", "branch": None}},
), ids=("answers-not-a-list", "answers-not-strings", "task-not-a-string", "task-null",
        "kind-null", "repository-not-an-object", "branch-null"))
def test_requirements_session_intake_types_are_validated(admin, change):
    body = {"repository": {"full_name": "owner/repo", "branch": "main"}, "kind": "docs",
            "answers": [], "task": "Document the API.", **change}
    response = admin.post("/requirements/sessions", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]
