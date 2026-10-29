"""GET /workflows filters and the fields it gains (FR-71 to FR-74, NFR-7), as far as the service
answers without Temporal: malformed filters are rejected with 422 before Temporal is contacted,
and well formed ones are not rejected."""
import uuid

import pytest


def onboarding_id() -> str:
    return f"onboarding-session-{uuid.uuid4().hex}"


def assert_unprocessable(response) -> None:
    assert response.status_code == 422, response.text
    assert response.json()["detail"], response.text


@pytest.mark.parametrize("repository", ("justaname", "owner/", "/name", "a/b/c"),
                         ids=("no-slash", "no-name", "no-owner", "too-many-parts"))
def test_repository_filter_must_be_owner_slash_name(admin, repository):
    assert_unprocessable(admin.get("/workflows", params={"repository": repository}))


def test_empty_branch_filter_is_rejected(admin):
    assert_unprocessable(admin.get("/workflows", params={"branch": ""}))


@pytest.mark.parametrize("value", (
    "onboarding-session-unknown",
    "onboarding-session-",
    f"onboarding-session-{uuid.uuid4().hex[:31]}",
    f"onboarding-session-{uuid.uuid4().hex}0",
    f"requirements-session-{uuid.uuid4().hex}",
    uuid.uuid4().hex,
), ids=("word", "no-digits", "31-digits", "33-digits", "other-prefix", "no-prefix"))
def test_onboarding_id_filter_must_be_an_onboarding_session_id(admin, value):
    assert_unprocessable(admin.get("/workflows", params={"onboarding_id": value}))


def test_malformed_filters_are_rejected_together_with_a_type(admin):
    assert_unprocessable(admin.get("/workflows", params={
        "type": "OnboardingWorkflow", "repository": "not-a-repository"}))


def test_one_malformed_filter_rejects_the_whole_request(admin):
    assert_unprocessable(admin.get("/workflows", params={
        "repository": "Owner/Repo", "branch": "main", "onboarding_id": "bad"}))


def test_well_formed_filters_are_not_rejected(admin):
    wanted = onboarding_id()
    branch = "feature/x"
    response = admin.get("/workflows", params={
        "type": "OnboardingWorkflow", "repository": "Some-Owner/Some-Repo", "branch": branch,
        "onboarding_id": wanted})
    # 200 with Temporal, 503 without it; neither is a malformed-filter 422.
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 503:
        assert isinstance(body["detail"], str) and body["detail"]
        return
    for item in body["workflows"]:
        assert item["workflow_type"] == "OnboardingWorkflow"
        assert item["onboarding_id"] == wanted
        assert item["branch"] == branch
        assert item["repository"]["full_name"].lower() == "some-owner/some-repo"


def test_executions_carry_the_new_fields_and_keep_the_old_ones(admin):
    response = admin.get("/workflows")
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 503:
        assert isinstance(body["detail"], str) and body["detail"]
        return
    for item in body["workflows"]:
        for old in ("workflow_id", "run_id", "workflow_type", "status", "task_queue", "start_time"):
            assert isinstance(item[old], str) and item[old], item
        # FR-74: always present, null when they do not apply.
        for new in ("repository", "branch", "onboarding_id"):
            assert new in item, item
        kind = item["workflow_type"]
        if kind in ("OnboardingWorkflow", "RequirementsWorkflow", "ApiDesignWorkflow",
                    "DeliveryWorkflow"):
            assert item["repository"]["full_name"] and item["branch"], item
            assert item["repository"]["branch"] == item["branch"], item
        if kind == "OnboardingWorkflow":
            assert item["onboarding_id"] == item["workflow_id"], item
        if item["onboarding_id"] is not None:
            assert item["onboarding_id"].startswith("onboarding-session-"), item


# FR-76: top-level flows and the children of one.
@pytest.mark.parametrize("params", (
    {"parent": ""},
    {"parent": " "},
    {"parent": "a\nb"},
    {"parent": "requirements-session-x", "top_level": "true"},
), ids=("empty", "blank", "control-character", "with-top-level"))
def test_malformed_parent_filter_is_rejected(admin, params):
    assert_unprocessable(admin.get("/workflows", params=params))


def test_top_level_lists_no_child_and_no_delivery(admin):
    response = admin.get("/workflows", params={"top_level": "true"})
    assert response.status_code in (200, 503), response.text
    if response.status_code == 503:
        return
    for item in response.json()["workflows"]:
        assert item["parent_workflow_id"] is None, item
        assert item["workflow_type"] != "DeliveryWorkflow", item


def test_parent_lists_only_its_children_and_delivery(admin):
    parent = f"requirements-session-{uuid.uuid4().hex}"
    response = admin.get("/workflows", params={"parent": parent})
    assert response.status_code in (200, 503), response.text
    if response.status_code == 503:
        return
    for item in response.json()["workflows"]:
        assert (item["parent_workflow_id"] == parent
                or item["workflow_id"] == f"delivery-{parent}"), item
