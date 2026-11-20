"""GET /workflows after the task flow change (FR-125, FR-149, FR-163, FR-164, FR-166, FR-167):
the `origin` filter and field, the RequirementsReviewWorkflow type, the removal of
`onboarding_id` and the pending/skipped statuses. Without Temporal the service answers 503;
when Temporal does answer, whatever executions come back must satisfy the contract."""
import pytest

ORIGINS = ("requirements", "onboarding")
STAGE_TYPES = {"RequirementsReviewWorkflow", "ArchitectureWorkflow", "ApiDesignWorkflow",
               "DeliveryWorkflow"}


def listed(admin, **params):
    """The executions of GET /workflows, or None while Temporal is unavailable (503)."""
    response = admin.get("/workflows", params=params)
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 503:
        assert isinstance(body["detail"], str) and body["detail"]
        return None
    return body["workflows"]


@pytest.mark.parametrize("origin", ORIGINS)
def test_origin_filter_lists_only_task_flows_of_that_origin(admin, origin):
    for item in listed(admin, origin=origin) or []:
        assert item["workflow_type"] == "TaskWorkflow", item
        assert item["origin"] == origin, item
        assert item["parent_workflow_id"] is None, item


@pytest.mark.parametrize("origin", ORIGINS)
def test_origin_combines_with_the_other_filters(admin, origin):
    executions = listed(admin, origin=origin, type="TaskWorkflow", top_level="true",
                        repository="Some-Owner/Some-Repo", branch="feature/x")
    for item in executions or []:
        assert item["origin"] == origin, item
        assert item["repository"]["full_name"].lower() == "some-owner/some-repo", item
        assert item["branch"] == "feature/x", item


def test_origin_never_matches_the_children_of_a_task_flow(admin):
    # A stage's own origin is null, and an execution whose field is null never matches.
    parent = "task-" + "0" * 32
    for item in listed(admin, origin="requirements", parent=parent) or []:
        assert item["origin"] == "requirements", item


def test_malformed_origin_is_rejected_before_any_other_filter_matters(admin):
    response = admin.get("/workflows", params={"origin": "chat", "type": "TaskWorkflow",
                                               "top_level": "true"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


def test_requirements_review_is_a_documented_workflow_type(admin):
    for item in listed(admin, type="RequirementsReviewWorkflow") or []:
        assert item["workflow_type"] == "RequirementsReviewWorkflow", item
        # A review is a child of its task flow, never top-level (FR-149).
        assert item["parent_workflow_id"], item
        assert item["repository"]["full_name"] and item["branch"], item
        assert item["origin"] is None and item["name"] is None, item


def test_task_flows_are_the_only_executions_with_an_origin_and_a_name(admin):
    for item in listed(admin) or []:
        assert "origin" in item and "name" in item, item
        if item["workflow_type"] == "TaskWorkflow":
            assert item["origin"] in ORIGINS, item
            assert isinstance(item["name"], str) and item["name"], item
        else:
            assert item["origin"] is None and item["name"] is None, item


def test_executions_no_longer_carry_an_onboarding_id(admin):
    for item in listed(admin) or []:
        assert "onboarding_id" not in item, item


@pytest.mark.parametrize("params", ({"type": "TaskWorkflow"}, {"top_level": "true"}, {}),
                         ids=("task-type", "top-level", "unfiltered"))
def test_no_stage_of_a_task_is_top_level_and_task_flows_have_no_parent(admin, params):
    top_level = params.get("top_level") == "true"
    for item in listed(admin, **params) or []:
        if item["workflow_type"] == "TaskWorkflow":
            assert item["parent_workflow_id"] is None, item
        elif top_level:
            assert item["workflow_type"] not in STAGE_TYPES, item


def test_pending_and_skipped_are_statuses_of_stages_only(admin):
    for item in listed(admin) or []:
        if item["status"] in ("pending", "skipped"):
            assert item["workflow_type"] != "TaskWorkflow", item
            assert item["parent_workflow_id"], item


def test_listing_with_an_origin_requires_sign_in(anonymous):
    response = anonymous.get("/workflows", params={"origin": "onboarding"})
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"
