"""Task flows (FR-125 to FR-143, NFR-7) as far as the service answers without Temporal, an LLM
provider or GitHub: starting a task, the fields every execution of GET /workflows carries, and the
listing filters for top-level flows and a flow's children."""
import uuid

import pytest

STAGE_TYPES = {"RequirementsWorkflow", "ArchitectureWorkflow", "ApiDesignWorkflow",
               "DeliveryWorkflow"}
CLOSED_STATUSES = {"completed", "cancelled", "failed", "terminated", "timed_out"}

VALID_INTAKE = {"repository": {"full_name": "owner/repo", "branch": "main"},
                "kind": "bugfix", "answers": [], "task": "Fix the rounding of prices."}


def workflows(admin, **params):
    """The executions GET /workflows lists: 200 with Temporal, 503 with a detail without it."""
    response = admin.get("/workflows", params=params)
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 503:
        assert isinstance(body["detail"], str) and body["detail"]
        return None
    assert isinstance(body["workflows"], list)
    return body["workflows"]


# Starting a task flow (FR-125, FR-128, FR-130, NFR-5)

def test_a_task_in_an_unreachable_repository_is_not_started(admin):
    # No GitHub App is connected, so the interview cannot check the repository out: no task
    # flow and no session may be created.
    body = dict(VALID_INTAKE, repository={
        "full_name": f"owner/none-{uuid.uuid4().hex[:10]}", "branch": "main"})
    response = admin.post("/requirements/sessions", json=body)
    assert response.status_code in (404, 409, 503), response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


def test_a_task_of_10000_characters_passes_validation(admin):
    body = dict(VALID_INTAKE, task="x" * 10000)
    response = admin.post("/requirements/sessions", json=body)
    # Refused later because no repository can be reached, not for its size.
    assert response.status_code in (404, 409, 503), response.text


@pytest.mark.parametrize("answers", ("yes", [1, 2], [None], {"0": "yes"}),
                         ids=("string", "numbers", "null-item", "object"))
def test_intake_answers_must_be_a_list_of_strings(admin, answers):
    response = admin.post("/requirements/sessions", json=dict(VALID_INTAKE, answers=answers))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


@pytest.mark.parametrize("repository", ({"full_name": "owner/repo", "branch": None},
                                        {"full_name": None, "branch": "main"}, "owner/repo",
                                        None),
                         ids=("null-branch", "null-name", "string", "null"))
def test_intake_repository_must_name_a_repository_and_branch(admin, repository):
    response = admin.post("/requirements/sessions", json=dict(VALID_INTAKE, repository=repository))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


# The fields of every execution (FR-74, FR-127, FR-128, FR-129, FR-135, NFR-7)

def test_task_flow_type_is_a_documented_type(admin):
    items = workflows(admin, type="TaskWorkflow")
    for item in items or []:
        assert item["workflow_type"] == "TaskWorkflow", item
        # A task flow has no parent and works in a repository.
        assert item.get("parent_workflow_id") is None, item
        assert item["repository"]["full_name"] and item["branch"], item
        assert item["repository"]["branch"] == item["branch"], item
        assert item["status"] not in ("pending", "skipped"), item


def test_every_execution_carries_the_task_flow_fields(admin):
    for item in workflows(admin) or []:
        for field in ("repository", "branch", "onboarding_id", "name", "linked",
                      "waiting_for_user", "unresolved_findings"):
            assert field in item, (field, item)
        assert isinstance(item["linked"], bool), item
        for field in ("waiting_for_user", "unresolved_findings"):
            assert item[field] is None or isinstance(item[field], bool), (field, item)
        kind = item["workflow_type"]
        if kind == "TaskWorkflow":
            assert isinstance(item["name"], str), item
        else:
            assert item["name"] is None, item
        if kind in STAGE_TYPES | {"TaskWorkflow", "OnboardingWorkflow"}:
            assert item["repository"]["full_name"] and item["branch"], item
        if item["status"] in ("pending", "skipped"):
            # Only the stages of a task flow can be pending or skipped.
            assert kind in STAGE_TYPES, item
            assert item.get("parent_workflow_id"), item
        if item["linked"]:
            assert item.get("parent_workflow_id"), item
        if item["status"] in CLOSED_STATUSES:
            # A closed flow is never waiting for the user (FR-110).
            assert item["waiting_for_user"] in (False, None), item


# Top-level flows and the children of a flow (FR-131)

def test_top_level_lists_task_flows_but_no_stage_of_a_task(admin):
    for item in workflows(admin, top_level="true") or []:
        assert item.get("parent_workflow_id") is None, item
        assert item["linked"] is False, item
        assert item["workflow_type"] not in ("ArchitectureWorkflow", "ApiDesignWorkflow",
                                             "DeliveryWorkflow"), item
        assert item["status"] not in ("pending", "skipped"), item


def test_task_flows_listed_for_the_type_are_top_level(admin):
    listed = workflows(admin, type="TaskWorkflow", top_level="true")
    if listed is not None:
        assert all(item["workflow_type"] == "TaskWorkflow" for item in listed)


def test_a_task_flow_that_does_not_exist_has_no_children(admin):
    items = workflows(admin, parent=f"task-{uuid.uuid4().hex}")
    if items is not None:
        assert items == []


def test_children_of_a_stage_are_never_other_stages_of_the_task(admin):
    parent = f"architecture-session-{uuid.uuid4().hex}"
    items = workflows(admin, parent=parent)
    for item in items or []:
        assert item["parent_workflow_id"] == parent, item
        assert item["workflow_type"] not in ("RequirementsWorkflow", "ArchitectureWorkflow",
                                             "ApiDesignWorkflow"), item


def test_parent_cannot_be_combined_with_top_level(admin):
    response = admin.get("/workflows", params={
        "parent": f"task-{uuid.uuid4().hex}", "top_level": "true"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


def test_parent_filter_is_combined_with_the_other_filters(admin):
    # A malformed filter rejects the request even when the parent is well formed.
    response = admin.get("/workflows", params={
        "parent": f"task-{uuid.uuid4().hex}", "repository": "not-a-repository"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


def test_invalid_page_token_is_not_accepted(admin):
    response = admin.get("/workflows", params={"page_token": "not-a-page-token"})
    # Rejected as malformed; 503 only when Temporal is unreachable.
    assert response.status_code in (422, 503), response.text
    assert response.json()["detail"]


def test_listing_workflows_requires_sign_in_for_every_filter(anonymous):
    for params in ({"type": "TaskWorkflow"}, {"top_level": "true"},
                   {"parent": f"task-{uuid.uuid4().hex}"}):
        response = anonymous.get("/workflows", params=params)
        assert response.status_code == 401, params
        assert response.headers["www-authenticate"] == "Bearer"


# Forks start a task flow of their own (FR-137): the session must exist first.

@pytest.mark.parametrize("prefix", (
    "/requirements/sessions/requirements-session-",
    "/architecture/sessions/architecture-session-",
    "/api-design/sessions/api-design-session-",
    "/onboarding/sessions/onboarding-session-"), ids=("requirements", "architecture",
                                                      "api-design", "onboarding"))
def test_forking_an_unknown_session_creates_no_flow(admin, prefix):
    response = admin.post(f"{prefix}{uuid.uuid4().hex}/fork", json={"message_index": 0})
    assert response.status_code == 404, response.text
    assert "task_flow_id" not in response.json()
    assert response.json()["detail"]
